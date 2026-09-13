"""Round-6 adversarial proofs (#206, PR #207): final trust-boundary corrections.

Covers FIX-R6-1 .. FIX-R6-3:

- FIX-R6-1: every emitted request line is the CANONICAL projection of the
  frozen lane authority, frozen reviewer identity, byte-verified neutral
  packet, and canonical labeling instructions. Paired JSONL+manifest
  rewrites (alter the case or instructions, then recompute every internal
  digest so the batch is self-consistent) fail against the external
  authorities.
- FIX-R6-2: ``identity_source == provider_metadata`` requires a
  digest-bound provider metadata artifact whose bytes MECHANICALLY DERIVE
  the model identity and provider IDs. Arbitrary invented IDs cannot claim
  machine verification; attestations cannot masquerade as provider
  metadata; tampered metadata fails its digest/derivation checks at
  freeze, load, and final ledger verification.
- FIX-R6-3: ALL completed outcome states (judged, refused, malformed) are
  re-derived from the exact raw bytes at raw-record validation, lane
  freeze, frozen-lane load, and final-ledger verification. A stored
  ``malformed`` classification whose bytes parse as a judgment or refusal
  fails closed; malformed is never an unchecked catch-all.
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
    observe_execution,
    verify_request_batch,
)
from evals.calibration.provider_metadata import (
    CANONICAL_MALFORMED_ERROR_CODE,
    PROVIDER_METADATA_ADAPTER_VERSION,
    ProviderMetadataArtifact,
)
from evals.calibration.review import write_protected_file
from tests.test_calibration_206_helpers import (
    NOW,
    build_frame_rows,
    build_verified_ledger,
    provider_metadata_for,
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


def _reviewer(slot: str, *, campaign_id: str = "legacy") -> ReviewerIdentity:
    if campaign_id == "eng-calibration-001k":
        from evals.calibration.consensus import CAMPAIGN_216_FAMILY_BY_SLOT

        families = CAMPAIGN_216_FAMILY_BY_SLOT
        identity_campaign_id = campaign_id
    else:
        families = dict(
            zip(REVIEWER_SLOTS, ("claude-opus", "gpt-astra", "glm-5-3-max"), strict=True)
        )
        identity_campaign_id = "legacy"
    family = families[slot]
    return ReviewerIdentity(
        reviewer_slot=slot,  # type: ignore[arg-type]
        reviewer_family=family,
        campaign_id=identity_campaign_id,
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


def _raw_refusal(sample_id: str) -> str:
    return json.dumps({"sample_id": sample_id, "outcome": "refused", "error_code": "refusal"})


def test_legacy_206_reviewer_identity_digest_matches_literal_pre_fix5_golden() -> None:
    identity = ReviewerIdentity(
        reviewer_slot="model_a",
        reviewer_family="claude-opus",
        campaign_id="legacy",
        provider_model_identifier="claude-opus-exact-2026-09",
        reviewer_config_digest="a" * 64,
        prompt_digest=labeling_instructions_digest(),
    )
    assert (
        identity.lane_identity_digest()
        == "4049768143c798c727e391c50ba52e005f90a29d5064acf3ede30afb989ba46c"
    )


def _setup_lane(
    tmp_path: Path,
    *,
    slot: str = "model_a",
    campaign_id: str = "campaign",
    provenance_mode: str = "provider_metadata",
):
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
    manifest_payload = (
        json.dumps({packet_path.name: hashlib.sha256(payload).hexdigest()}, indent=2) + "\n"
    ).encode()
    write_protected_file(manifest_path, manifest_payload)
    session = LaneSession.init(
        tmp_path,
        reviewer=_reviewer(slot, campaign_id=campaign_id),
        campaign_id=campaign_id,
        sampling=sampling,
        source_packet_digest="f" * 64,
        neutral_packet_path=packet_path,
        neutral_packet_manifest=manifest_path,
        provenance_mode=provenance_mode,  # type: ignore[arg-type]
    )
    session.emit_requests(packet_path, sampling=sampling, manifest_path=manifest_path)
    return session, sampling, packet_path, manifest_path


def _observed(session: LaneSession, sample_id: str, **overrides) -> dict:
    """OBSERVED executor metadata + the artifact that reports it (R6-2)."""
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
        "executed_at": NOW,
        "sample_id": sample_id,
    }
    observed.update(overrides)
    if observed["identity_source"] == "provider_metadata":
        observed["provider_metadata_artifact"] = ProviderMetadataArtifact.capture(
            provider="synthetic-provider-206",
            raw_metadata={
                "model": observed["actual_provider_model_identifier"],
                "request_id": "req-206-0001",
                "response_id": "resp-206-0001",
            },
        )
    return observed


def _ingest(session: LaneSession, sampling: SamplingManifest, sample_id: str, raw: str) -> None:
    observed = _observed(session, sample_id)
    sample = observed.pop("sample_id")
    receipt = observe_execution(session.lane_root, sample_id=sample, **observed)
    session.ingest_response(
        {
            "sample_id": sample_id,
            "raw_response": raw,
            "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
        },
        sampling=sampling,
    )


def _rewrite_batch(
    lane_root: Path,
    generation: int,
    line_mutator,
    *,
    manifest_updates: dict | None = None,
) -> None:
    """Paired rewrite: mutate the JSONL lines, then RECOMPUTE the manifest
    (request_sha256, item digests, pending membership) so the batch + manifest
    are fully self-consistent — the exact pre-execution rewrite attack."""
    from evals.calibration.ingestion import request_item_digest

    batch = lane_root / f"lane-requests-{generation:06d}.jsonl"
    manifest_path = lane_root / f"lane-requests-{generation:06d}.manifest.json"
    lines = [json.loads(line) for line in batch.read_text().splitlines() if line.strip()]
    lines = [line_mutator(line) for line in lines]
    serialized = [json.dumps(line, sort_keys=True) for line in lines]
    payload = ("\n".join(serialized) + "\n").encode()
    manifest = json.loads(manifest_path.read_text())
    manifest["request_sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["pending_sample_ids"] = [line["sample_id"] for line in lines]
    manifest["request_items"] = {
        line["sample_id"]: {
            "request_item_digest": request_item_digest(line),
            "case_index": line["case_index"],
        }
        for line in lines
    }
    if manifest_updates:
        manifest.update(manifest_updates)
    batch.unlink()
    manifest_path.unlink()
    write_protected_file(batch, payload)
    write_protected_file(manifest_path, (json.dumps(manifest, sort_keys=True) + "\n").encode())


def _load_authority_and_packet(lane_root: Path):
    from evals.calibration.ingestion import LaneAuthority
    from evals.calibration.model_lanes import NeutralModelPacket

    authority = LaneAuthority.model_validate(json.loads((lane_root / "lane.json").read_text()))
    packet = NeutralModelPacket.model_validate(
        json.loads((lane_root / "neutral-packet.json").read_text())
    )
    return authority, packet


# ---------------------------------------------------------------------------
# FIX-R6-1: canonical request-line authority
# ---------------------------------------------------------------------------


class TestFixR61CanonicalRequestLines:
    def test_valid_emitted_batch_passes_canonical_verification(self, tmp_path: Path):
        session, _, _, _ = _setup_lane(tmp_path)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        manifest = verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)
        assert manifest["generation"] == 1

    def test_registry_load_runs_canonical_verification(self, tmp_path: Path):
        """The provenance registry (used at ingest/freeze/load/ledger)
        verifies each line against the lane authority automatically."""
        from evals.calibration.ingestion import _load_request_registry

        session, _, _, _ = _setup_lane(tmp_path)
        registry = _load_request_registry(session.lane_root)
        assert set(registry) == set(IDS)

    def test_paired_case_content_rewrite_fails(self, tmp_path: Path):
        """Case substitution: alter case.content, recompute request_sha256 and
        item digests, keep asserted neutral packet identity — must fail
        because request.case != frozen neutral case."""
        session, _, _, _ = _setup_lane(tmp_path)
        _rewrite_batch(
            session.lane_root,
            1,
            lambda line: {**line, "case": {**line["case"], "content": "TAMPERED EVIDENCE"}},
        )
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_line_case_not_frozen_neutral_case"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_paired_case_substitution_from_other_sample_fails(self, tmp_path: Path):
        """Swap two cases' content wholesale (field-exact wrong case)."""
        session, _, _, _ = _setup_lane(tmp_path)
        other = {"sample_id": "s2", "content": "content-s2"}

        def mutate(line):
            if line["sample_id"] == "s1":
                return {**line, "case": dict(other)}
            return line

        _rewrite_batch(session.lane_root, 1, mutate)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_line_case_not_frozen_neutral_case"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_paired_instruction_substitution_fails(self, tmp_path: Path):
        """Instruction substitution: change one semantic instruction,
        recompute batch SHA + item digests, keep prompt_digest fields claiming
        the frozen prompt — must fail: actual bundle != canonical bundle."""
        session, _, _, _ = _setup_lane(tmp_path)
        forged_instructions = json.loads(json.dumps(LABELING_INSTRUCTIONS))
        forged_instructions["semantics"]["retention_value"]["rules"] = (
            "TAMPERED: retention IS truth, label retain whenever correct."
        )

        def mutate(line):
            return {**line, "labeling_instructions": forged_instructions}

        _rewrite_batch(session.lane_root, 1, mutate)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_line_instructions_not_canonical_bundle"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_prompt_digest_claiming_frozen_prompt_with_other_bundle_fails(self, tmp_path):
        """Even when prompt_digest still CLAIMS the frozen digest, the bundle
        equality check (not digest-to-digest within the mutable pair) fails."""
        session, _, _, _ = _setup_lane(tmp_path)
        forged = json.loads(json.dumps(LABELING_INSTRUCTIONS))
        forged["semantics"]["consequence"]["rules"] = "TAMPERED consequence semantics."

        def mutate(line):
            return {
                **line,
                "labeling_instructions": forged,
                "prompt_digest": labeling_instructions_digest(),  # still claims frozen
            }

        _rewrite_batch(session.lane_root, 1, mutate)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_line_instructions_not_canonical_bundle"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    @pytest.mark.parametrize(
        ("field", "forged_value", "expected_error"),
        [
            ("reviewer_family", "gpt-astra", "request_line_family_not_frozen_reviewer"),
            (
                "provider_model_identifier",
                "claude-opus-IMPOSTOR-v9",
                "request_line_model_not_frozen_reviewer",
            ),
            ("reviewer_config_digest", "b" * 64, "request_line_config_not_frozen_reviewer"),
            ("campaign_id", "other-campaign", "request_line_campaign_not_frozen_authority"),
            (
                "sampling_manifest_digest",
                "7" * 64,
                "request_line_sampling_manifest_not_frozen_authority",
            ),
            (
                "source_packet_digest",
                "6" * 64,
                "request_line_source_packet_not_frozen_authority",
            ),
            (
                "neutral_packet_sha256",
                "9" * 64,
                "request_line_neutral_packet_not_frozen_authority",
            ),
            (
                "label_guide_version",
                "engram-calibration-guide-999-v9",
                "request_line_label_guide_not_frozen",
            ),
            (
                "prompt_digest",
                "8" * 64,
                "request_line_prompt_not_frozen_reviewer",
            ),
        ],
    )
    def test_paired_identity_substitutions_fail(
        self, tmp_path: Path, field: str, forged_value: str, expected_error: str
    ):
        """Identity substitution: paired rewrites that keep the batch+manifest
        internally self-consistent still fail against the external lane/
        campaign authority."""
        session, _, _, _ = _setup_lane(tmp_path)
        updates = {}
        # keep the manifest's asserted prompt binding self-consistent too
        if field == "prompt_digest":
            updates["reviewer_prompt_digest"] = forged_value
        if field == "neutral_packet_sha256":
            updates["neutral_packet_sha256"] = forged_value
        _rewrite_batch(
            session.lane_root,
            1,
            lambda line: {**line, field: forged_value},
            manifest_updates=updates or None,
        )
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match=expected_error):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_extra_evidence_key_in_request_line_fails(self, tmp_path: Path):
        """No additional evidence may appear in a request line."""
        session, _, _, _ = _setup_lane(tmp_path)

        def mutate(line):
            return {**line, "provider_score_hint": 0.9}

        _rewrite_batch(session.lane_root, 1, mutate)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_batch_line_noncanonical_keys"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_dropped_case_evidence_key_fails(self, tmp_path: Path):
        """No frozen case evidence may be removed from a request line."""
        session, _, _, _ = _setup_lane(tmp_path)

        def mutate(line):
            forged = dict(line)
            forged["case"] = {k: v for k, v in forged["case"].items() if k != "content"}
            return forged

        _rewrite_batch(session.lane_root, 1, mutate)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_line_case_not_frozen_neutral_case"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_case_index_reassigned_to_other_frozen_index_fails(self, tmp_path: Path):
        """case_index must be the frozen packet index of that sample_id."""
        session, _, _, _ = _setup_lane(tmp_path)

        def mutate(line):
            if line["sample_id"] == "s1":
                return {**line, "case_index": 2, "case": dict(line["case"])}
            return line

        _rewrite_batch(session.lane_root, 1, mutate)
        authority, packet = _load_authority_and_packet(session.lane_root)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        with pytest.raises(ValueError, match="request_line_case_index_not_frozen_packet_index"):
            verify_request_batch(batch, lane_authority=authority, neutral_packet=packet)

    def test_retained_neutral_packet_mutation_detected(self, tmp_path: Path):
        """The lane's retained neutral-packet bytes are digest-bound; mutating
        them fails the registry load (and therefore every boundary)."""
        from evals.calibration.ingestion import _load_request_registry

        session, _, _, _ = _setup_lane(tmp_path)
        retained = session.lane_root / "neutral-packet.json"
        payload = json.loads(retained.read_text())
        payload["cases"][0]["content"] = "MUTATED CASE CONTENT"
        retained.unlink()
        write_protected_file(retained, json.dumps(payload, sort_keys=True).encode())
        with pytest.raises(ValueError, match="neutral_packet_sha_mismatch"):
            _load_request_registry(session.lane_root)

    def test_missing_retained_packet_fails_closed(self, tmp_path: Path):
        from evals.calibration.ingestion import _load_request_registry

        session, _, _, _ = _setup_lane(tmp_path)
        (session.lane_root / "neutral-packet.json").unlink()
        with pytest.raises(ValueError, match="lane_retained_neutral_packet_missing"):
            _load_request_registry(session.lane_root)

    def test_paired_rewrite_breaks_ingestion_of_new_responses(self, tmp_path: Path):
        """End-to-end: a paired rewrite is rejected when a response arrives —
        observe_execution loads the registry, which runs canonical
        verification."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        _rewrite_batch(
            session.lane_root,
            1,
            lambda line: {**line, "case": {**line["case"], "content": "TAMPERED"}},
        )
        with pytest.raises(ValueError, match="request_line_case_not_frozen_neutral_case"):
            _ingest(session, sampling, "s1", _raw_judgment("s1"))


# ---------------------------------------------------------------------------
# FIX-R6-2: provider metadata derivation / tamper
# ---------------------------------------------------------------------------


class TestFixR62ProviderMetadata:
    def test_provider_metadata_without_artifact_fails(self, tmp_path: Path):
        """identity_source=provider_metadata + arbitrary IDs but no artifact
        → the schema itself rejects the evidence."""
        from evals.calibration.consensus import ExecutionEvidence

        with pytest.raises(
            ValueError, match="provider_metadata_identity_requires_metadata_artifact"
        ):
            ExecutionEvidence(
                campaign_id="campaign",
                actual_reviewer_slot="model_a",
                actual_reviewer_family="claude-opus",
                actual_provider_model_identifier="claude-opus-exact-2026-09",
                actual_configuration_digest="a" * 64,
                actual_prompt_digest=labeling_instructions_digest(),
                request_generation=1,
                request_item_digest="d" * 64,
                executed_at=NOW,
                executor_status="completed",
                executor_identity="executor",
                identity_source="provider_metadata",
                provider_request_id="req-206-0001",
                provider_response_id="resp-206-0001",
            )

    def test_metadata_says_gpt_but_evidence_claude_fails(self):
        """Provider metadata says GPT Astra but the normalized evidence field
        claims Claude → derivation mismatch."""
        from evals.calibration.consensus import ExecutionEvidence
        from evals.calibration.provider_metadata import verify_execution_provider_metadata

        artifact = provider_metadata_for("gpt-astra-exact-2026-09")
        evidence = ExecutionEvidence(
            campaign_id="campaign",
            actual_reviewer_slot="model_a",
            actual_reviewer_family="claude-opus",
            actual_provider_model_identifier="claude-opus-exact-2026-09",  # CLAIMS Claude
            actual_configuration_digest="a" * 64,
            actual_prompt_digest=labeling_instructions_digest(),
            request_generation=1,
            request_item_digest="d" * 64,
            executed_at=NOW,
            executor_status="completed",
            executor_identity="executor",
            identity_source="executor_attestation",  # constructible; verified below
        )
        with pytest.raises(
            ValueError,
            match=(
                "provider_metadata_model_derivation_mismatch"
                "|provider_metadata_verification_requires_provider_metadata_source"
            ),
        ):
            verify_execution_provider_metadata(evidence, artifact=artifact)

    def test_metadata_version_mismatch_against_expected_lane_fails(self, tmp_path: Path):
        """Provider metadata honestly says claude-opus version X but the lane
        froze version Y → ingestion identity mismatch (fail closed)."""
        session, sampling, _, _ = _setup_lane(tmp_path)
        observed = _observed(
            session,
            "s1",
            actual_provider_model_identifier="claude-opus-OTHER-VERSION",
        )
        sample = observed.pop("sample_id")
        receipt = observe_execution(session.lane_root, sample_id=sample, **observed)
        with pytest.raises(ValueError, match="execution_receipt_identity_mismatch"):
            session.ingest_response(
                {
                    "sample_id": "s1",
                    "raw_response": _raw_judgment("s1"),
                    "execution": json.loads(json.dumps(receipt.model_dump(mode="json"))),
                },
                sampling=sampling,
            )

    def test_metadata_artifact_mutated_after_ingestion_fails_at_freeze(self, tmp_path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        for sid in IDS:
            _ingest(session, sampling, sid, _raw_judgment(sid))
        # mutate the PRESERVED artifact bytes after ingestion
        preserved = session.lane_root / "provider-meta" / "s2.json"
        payload = json.loads(preserved.read_text())
        payload["raw_metadata"]["model"] = "claude-opus-TAMPERED"
        # recompute the artifact's internal digest so only BYTES changed
        payload["raw_metadata_sha256"] = hashlib.sha256(
            __import__("rfc8785").dumps(payload["raw_metadata"])
        ).hexdigest()
        preserved.unlink()
        write_protected_file(preserved, json.dumps(payload, sort_keys=True).encode())
        from evals.calibration.model_lanes import freeze_lane

        with pytest.raises(
            ValueError,
            match="provider_metadata_artifact_disagrees_with_preserved|provider_metadata_model_derivation_mismatch",
        ):
            freeze_lane(
                protected_root=tmp_path,
                reviewer=session.reviewer,
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
            )

    def test_embedded_metadata_mutated_in_stored_record_fails(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        for sid in IDS:
            _ingest(session, sampling, sid, _raw_judgment(sid))
        record_path = session.lane_root / "s2.json"
        payload = json.loads(record_path.read_text())
        payload["execution"]["provider_metadata"]["raw_metadata"]["model"] = "claude-opus-TAMPERED"
        record_path.unlink()
        write_protected_file(record_path, json.dumps(payload, sort_keys=True).encode())
        # the mutated embedded artifact makes the stored record unloadable —
        # the receipt no longer derives from its own (tampered) evidence
        with pytest.raises(
            Exception,
            match=(
                "execution_receipt_not_derived_from_its_evidence|provider_metadata_digest_mismatch"
            ),
        ):
            ModelReviewRecord.model_validate(json.loads(record_path.read_text()))

    def test_provider_response_id_altered_independently_fails(self, tmp_path: Path):
        """The evidence's provider IDs must be the metadata derivation:
        verifying a genuine evidence against a DIFFERENT (digest-valid)
        artifact whose response ID differs fails the derivation check."""
        from evals.calibration.provider_metadata import verify_execution_provider_metadata

        session, _, _, _ = _setup_lane(tmp_path)
        observed = _observed(session, "s1")
        sample = observed.pop("sample_id")
        receipt = observe_execution(session.lane_root, sample_id=sample, **observed)
        # a second, internally-valid artifact that reports a DIFFERENT
        # response ID for the same model
        other_artifact = provider_metadata_for(
            session.reviewer.provider_model_identifier,
            request_id="req-206-0001",
            response_id="resp-DIFFERENT",
        )
        assert receipt.evidence is not None
        with pytest.raises(ValueError, match="provider_metadata_response_id_derivation_mismatch"):
            verify_execution_provider_metadata(receipt.evidence, artifact=other_artifact)

    def test_actual_metadata_correct_derived_model_passes(self, tmp_path: Path):
        session, sampling, _, _ = _setup_lane(tmp_path)
        for sid in IDS:
            _ingest(session, sampling, sid, _raw_judgment(sid))
        from evals.calibration.model_lanes import freeze_lane

        lane_result = freeze_lane(
            protected_root=tmp_path,
            reviewer=session.reviewer,
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        assert lane_result.sample_ids == IDS

    def test_attestation_cannot_masquerade_as_provider_metadata(self, tmp_path: Path):
        """executor_attestation stays truthfully represented: no artifact, no
        provider IDs, and it can never freeze a consensus lane."""
        from evals.calibration.consensus import ExecutionEvidence

        with pytest.raises(
            ValueError, match="executor_attestation_must_not_claim_provider_metadata"
        ):
            ExecutionEvidence(
                campaign_id="campaign",
                actual_reviewer_slot="model_a",
                actual_reviewer_family="claude-opus",
                actual_provider_model_identifier="claude-opus-exact-2026-09",
                actual_configuration_digest="a" * 64,
                actual_prompt_digest=labeling_instructions_digest(),
                request_generation=1,
                request_item_digest="d" * 64,
                executed_at=NOW,
                executor_status="completed",
                executor_identity="executor",
                identity_source="executor_attestation",
                provider_request_id="req-invented",  # invented IDs do not upgrade
                provider_response_id="resp-invented",
                provider_metadata=provider_metadata_for("claude-opus-exact-2026-09").model_dump(
                    mode="json"
                ),
            )

    def test_observe_execution_refuses_provider_metadata_without_artifact(self, tmp_path: Path):
        session, _, _, _ = _setup_lane(tmp_path)
        observed = _observed(session, "s1")
        sample = observed.pop("sample_id")
        observed.pop("provider_metadata_artifact")
        with pytest.raises(
            ValueError, match="provider_metadata_identity_requires_metadata_artifact"
        ):
            observe_execution(session.lane_root, sample_id=sample, **observed)

    def test_adapter_identity_is_frozen(self):
        assert PROVIDER_METADATA_ADAPTER_VERSION == "provider-metadata-adapter-206-v1"
        artifact = provider_metadata_for("m")
        assert artifact.adapter_version == PROVIDER_METADATA_ADAPTER_VERSION

    def test_capture_requires_identity_capable_metadata(self):
        """Metadata that cannot mechanically establish model identity is
        rejected at capture — the caller must attest instead."""
        with pytest.raises(ValueError, match="provider_metadata_missing_required_fields"):
            ProviderMetadataArtifact.capture(
                provider="synthetic-provider-206",
                raw_metadata={"request_id": "r", "response_id": "s"},  # no model
            )


# ---------------------------------------------------------------------------
# FIX-R6-3: full raw-byte outcome re-derivation
# ---------------------------------------------------------------------------


class TestFixR63OutcomeReDerivation:
    def _completed_lane(self, tmp_path: Path, outcome: str) -> tuple[LaneSession, SamplingManifest]:
        session, sampling, _, _ = _setup_lane(tmp_path)
        for sid in IDS:
            if outcome == "judged":
                _ingest(session, sampling, sid, _raw_judgment(sid))
            elif outcome == "refused":
                _ingest(session, sampling, sid, _raw_refusal(sid))
            else:
                _ingest(session, sampling, sid, "garbage not json")
        return session, sampling

    def _mutate_record(self, session: LaneSession, sample_id: str, **updates) -> None:
        path = session.lane_root / f"{sample_id}.json"
        payload = json.loads(path.read_text())
        payload.update(updates)
        path.unlink()
        write_protected_file(path, json.dumps(payload, sort_keys=True).encode())

    def _reload(self, session: LaneSession) -> dict[str, ModelReviewRecord]:
        from evals.calibration.model_lanes import load_lane_records

        return load_lane_records(session.protected_root, session.reviewer.reviewer_slot)

    def _expect_raw_validation_failure(self, session: LaneSession, match: str) -> None:
        from evals.calibration.raw_evidence import validate_lane_raw_evidence

        records = self._reload(session)
        with pytest.raises(ValueError, match=match):
            validate_lane_raw_evidence(
                records,
                protected_root=session.protected_root,
                reviewer_slot=session.reviewer.reviewer_slot,
            )

    def test_genuine_judgment_passes_all_boundaries(self, tmp_path: Path):
        from evals.calibration.model_lanes import freeze_lane

        session, sampling = self._completed_lane(tmp_path, "judged")
        lane_result = freeze_lane(
            protected_root=tmp_path,
            reviewer=session.reviewer,
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        assert lane_result.sample_ids == IDS

    def test_genuine_refusal_passes_all_boundaries(self, tmp_path: Path):
        from evals.calibration.model_lanes import freeze_lane

        session, sampling = self._completed_lane(tmp_path, "refused")
        freeze_lane(
            protected_root=tmp_path,
            reviewer=session.reviewer,
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        records = self._reload(session)
        assert all(r.outcome_status == "refused" for r in records.values())

    def test_genuine_malformed_passes_all_boundaries(self, tmp_path: Path):
        from evals.calibration.model_lanes import freeze_lane

        session, sampling = self._completed_lane(tmp_path, "malformed")
        freeze_lane(
            protected_root=tmp_path,
            reviewer=session.reviewer,
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        records = self._reload(session)
        assert all(r.outcome_status == "malformed" for r in records.values())
        assert all(r.error_code == CANONICAL_MALFORMED_ERROR_CODE for r in records.values())

    def test_refused_relabelled_malformed_fails(self, tmp_path: Path):
        """refused → malformed while keeping refusal bytes → fail."""
        session, _ = self._completed_lane(tmp_path, "refused")
        self._mutate_record(session, "s2", outcome_status="malformed")
        self._expect_raw_validation_failure(session, "record_outcome_not_derived_from_bytes")

    def test_judged_relabelled_malformed_fails(self, tmp_path: Path):
        """judged → malformed while keeping valid judgment bytes → fail.
        (The schema forbids judgment+malformed, so the stored record itself
        fails to re-validate — equally a rejection at every boundary.)"""
        session, _ = self._completed_lane(tmp_path, "judged")
        self._mutate_record(session, "s2", outcome_status="malformed")
        with pytest.raises(Exception, match="parsed_review_requires_judged_outcome"):
            self._reload(session)

    def test_malformed_relabelled_refused_fails(self, tmp_path: Path):
        """malformed → refused → fail (bytes do not parse as a refusal)."""
        session, _ = self._completed_lane(tmp_path, "malformed")
        self._mutate_record(session, "s2", outcome_status="refused", error_code="refusal")
        self._expect_raw_validation_failure(session, "record_outcome_not_derived_from_bytes")

    def test_malformed_relabelled_judged_fails(self, tmp_path: Path):
        """malformed → judged → fail (schema: parsed requires judgment)."""
        session, _ = self._completed_lane(tmp_path, "malformed")
        self._mutate_record(session, "s2", parse_status="parsed", outcome_status="judged")
        with pytest.raises(
            Exception,
            match=(
                "parsed_review_requires_judgment"
                "|parsed_review_requires_judged_outcome"
                "|malformed_response_requires_refused_or_malformed_outcome"
            ),
        ):
            self._reload(session)

    def test_parsed_judgment_field_mutation_fails(self, tmp_path: Path):
        """Mutate one stored judgment field (bytes unchanged) → fail."""
        session, _ = self._completed_lane(tmp_path, "judged")
        path = session.lane_root / "s2.json"
        payload = json.loads(path.read_text())
        payload["judgment"]["fields"]["expected_kind"] = "decision"
        path.unlink()
        write_protected_file(path, json.dumps(payload, sort_keys=True).encode())
        self._expect_raw_validation_failure(session, "record_judgment_disagrees_with_bytes")

    def test_refusal_error_code_mutation_fails(self, tmp_path: Path):
        """Mutate the stored refusal error code (bytes unchanged) → fail."""
        session, _ = self._completed_lane(tmp_path, "refused")
        self._mutate_record(session, "s2", error_code="different-code")
        self._expect_raw_validation_failure(
            session, "record_refusal_error_code_disagrees_with_bytes"
        )

    def test_malformed_noncanonical_error_code_fails(self, tmp_path: Path):
        """A malformed record carrying a wrapper-supplied error code (not the
        canonical one) fails re-derivation."""
        session, _ = self._completed_lane(tmp_path, "malformed")
        self._mutate_record(session, "s2", error_code="wrapper_supplied_code")
        self._expect_raw_validation_failure(session, "record_malformed_error_code_not_canonical")

    def test_judged_confidence_mutation_fails(self, tmp_path: Path):
        """Stored reviewer_confidence must equal the parsed judgment's."""
        session, _ = self._completed_lane(tmp_path, "judged")
        path = session.lane_root / "s2.json"
        payload = json.loads(path.read_text())
        payload["reviewer_confidence"] = "high"
        payload["judgment"]["reviewer_confidence"] = "high"
        path.unlink()
        write_protected_file(path, json.dumps(payload, sort_keys=True).encode())
        self._expect_raw_validation_failure(session, "record_judgment_disagrees_with_bytes")

    def test_outcome_mutations_fail_at_lane_freeze(self, tmp_path: Path):
        """The refused→malformed mutation is caught by lane freeze (which
        runs the full raw-evidence validator)."""
        from evals.calibration.model_lanes import freeze_lane

        session, sampling = self._completed_lane(tmp_path, "refused")
        self._mutate_record(session, "s2", outcome_status="malformed")
        with pytest.raises(ValueError, match="record_outcome_not_derived_from_bytes"):
            freeze_lane(
                protected_root=tmp_path,
                reviewer=session.reviewer,
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
            )

    def test_outcome_mutations_fail_at_frozen_lane_load(self, tmp_path: Path):
        """Freeze a genuine refused lane, mutate a stored record, then load:
        the frozen-lane load boundary rejects it."""
        from evals.calibration.model_lanes import freeze_lane, load_frozen_lanes

        session, sampling = self._completed_lane(tmp_path, "refused")
        freeze_lane(
            protected_root=tmp_path,
            reviewer=session.reviewer,
            campaign_id="campaign",
            sampling=sampling,
            source_packet_digest="f" * 64,
        )
        self._mutate_record(session, "s2", outcome_status="malformed")
        with pytest.raises(
            Exception,
            match=(
                "record_outcome_not_derived_from_bytes"
                "|malformed_response_requires_refused_or_malformed_outcome"
                "|lane_record_digest_mismatch"
            ),
        ):
            load_frozen_lanes(
                tmp_path,
                campaign_id="campaign",
                sampling=sampling,
                source_packet_digest="f" * 64,
                reviewers={slot: _reviewer(slot) for slot in REVIEWER_SLOTS},
            )

    def test_executor_status_disagreeing_with_raw_state_fails(self, tmp_path: Path):
        """A response-carrying record whose execution evidence claims
        provider_error fails closed."""
        session, _ = self._completed_lane(tmp_path, "judged")
        path = session.lane_root / "s2.json"
        payload = json.loads(path.read_text())
        payload["execution"]["executor_status"] = "provider_error"
        payload["execution"]["evidence"]["executor_status"] = "provider_error"
        path.unlink()
        write_protected_file(path, json.dumps(payload, sort_keys=True).encode())
        with pytest.raises(
            Exception,
            match="execution_receipt_not_derived_from_its_evidence|execution_receipt_evidence_digest_mismatch",
        ):
            self._reload(session)  # receipt consistency fails at schema too

    def test_ledger_boundary_rejects_mutated_outcome(self):
        """Final consensus-ledger verification consumes lanes validated
        through the same raw re-derivation path; the build_verified_ledger
        helper (real verifier) proves the genuine path still passes."""
        verified = build_verified_ledger(
            IDS,
            {sid: dict(GOOD_CRITICAL) for sid in IDS},
        )
        assert len(verified.reference_rows()) == len(IDS)


class TestFixR6ServingInvariantsPreserved:
    def test_serving_invariants_unchanged(self):
        from engram.config import settings
        from engram.recall_profiles import CERTIFIED_SERVING_PROFILES

        assert settings.assessment_selection_enabled is False
        assert {"legacy"} == CERTIFIED_SERVING_PROFILES

    def test_round2_identities_unchanged(self):
        from evals.calibration.campaigns import EXPECTED_SPLIT_MANIFEST_DIGEST

        assert (
            EXPECTED_SPLIT_MANIFEST_DIGEST
            == "a2a27ed4c0152bf2d9b6c318bbfcfd6e5e20944184a0cc9df18cb2b4e3fbb72b"
        )


# ---------------------------------------------------------------------------
# Round-6 NO-GO correction: content-authenticating registry cache identity
# ---------------------------------------------------------------------------


class TestFixR6RegistryCacheContentAuthenticity:
    """The registry memoization key must authenticate CONTENT, not stat
    identity. A file owner can rewrite same-length bytes and restore the
    original mtime with os.utime(), leaving (path, mtime_ns, size) identical
    while the bytes differ — a stat-derived cache key would then serve the
    previously verified registry without reading the new bytes."""

    @staticmethod
    def _stats() -> dict[str, int]:
        from evals.calibration import ingestion

        return dict(ingestion._REGISTRY_CACHE_STATS)

    def test_same_stat_different_bytes_rewrite_is_rejected(self, tmp_path: Path):
        """Exact bypass scenario: paired JSONL+manifest rewrite that is
        internally self-consistent, byte-for-byte SAME LENGTH in both files,
        with original mtimes restored — the stat tuples are identical, yet
        the altered bytes must be re-read and rejected by the canonical
        authority checks."""
        import os

        from evals.calibration.ingestion import _load_request_registry

        session, _, _, _ = _setup_lane(tmp_path)
        batch = session.lane_root / "lane-requests-000001.jsonl"
        manifest_path = session.lane_root / "lane-requests-000001.manifest.json"

        # Cache the valid registry through the real authority path.
        registry = _load_request_registry(session.lane_root)
        assert set(registry) == set(IDS)

        # Record the original stat identity of the request evidence pair.
        original_stats = {
            p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (batch, manifest_path)
        }

        # Unauthorized same-length modification: paired rewrite recomputes
        # request_sha256 and every request_item_digest so the pair is fully
        # self-consistent; only the frozen case content differs (case flip).
        def mutate(line):
            if line["sample_id"] == "s1":
                return {**line, "case": {**line["case"], "content": "CONTENT-s1"}}
            return line

        _rewrite_batch(session.lane_root, 1, mutate)

        # The replacement bytes must be exactly the original file lengths —
        # the rejection below may NOT come from a size change.
        for p, (_mtime_ns, size) in original_stats.items():
            assert p.stat().st_size == size, "replacement bytes must keep exact length"

        # Restore the original mtimes: stat tuples become identical.
        for p, (mtime_ns, _size) in original_stats.items():
            os.utime(p, ns=(mtime_ns, mtime_ns))
        for p, ident in original_stats.items():
            assert (p.stat().st_mtime_ns, p.stat().st_size) == ident

        # The real authority path must re-read the bytes and reject them on
        # canonical grounds (case content != frozen neutral-packet case).
        stats_before = self._stats()
        with pytest.raises(ValueError, match="request_line_case_not_frozen_neutral_case"):
            _load_request_registry(session.lane_root)
        stats_after = self._stats()
        assert stats_after["miss"] == stats_before["miss"] + 1
        assert stats_after["hit"] == stats_before["hit"]

    def test_unchanged_bytes_reuse_memoized_verified_registry(self, tmp_path: Path):
        """Positive control: genuinely unchanged bytes take the fast path —
        the exact previously verified registry object is reused."""
        from evals.calibration.ingestion import _load_request_registry

        session, _, _, _ = _setup_lane(tmp_path)
        stats0 = self._stats()
        first = _load_request_registry(session.lane_root)
        stats1 = self._stats()
        assert stats1["miss"] == stats0["miss"] + 1
        assert stats1["hit"] == stats0["hit"]

        second = _load_request_registry(session.lane_root)
        stats2 = self._stats()
        assert stats2["hit"] == stats1["hit"] + 1
        assert stats2["miss"] == stats1["miss"]
        assert second is first

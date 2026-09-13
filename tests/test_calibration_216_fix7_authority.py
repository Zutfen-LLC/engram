"""FIX7 (#217) adversarial regressions: single-path direct reviewer authority.

Every proof here is an attack on the FIX7 hardening itself: alternate
reviewer-provenance paths for active 001k evidence, forged structural retry
authorization over retained bytes that mechanically parse, request-identity
mutation, and the inverse (a terminal classification over mechanically
malformed bytes).  All forgeries are built by mutating a GENUINE attempt's
fields and re-deriving local digests — exactly what a tampered on-disk
artifact looks like.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evals.calibration.api_reviewer_216 import (
    DirectAPIReviewAttempt216,
    HTTPResponseCapture,
    verify_direct_api_attempt_216,
)
from evals.calibration.campaign_001k import (
    ACTIVE_001K_PROVENANCE_MODES,
    CAMPAIGN_ID_001K,
    require_active_001k_provenance_mode,
)
from tests.test_calibration_216_api_reviewer import (
    _SAMPLE,
    FakeTransport,
    _judged_content,
    _reviewer,
)
from tests.test_calibration_216_api_reviewer import (
    _chat_response as _chat,
)

_FIELDS = {
    "expected_kind": "fact",
    "retention_value": "retain",
    "epistemic_state": "adequately_supported",
    "consequence": "low",
    "acceptable_abstention": "no",
}


def _refused_content(sample_id: str = _SAMPLE["sample_id"]) -> str:
    return json.dumps({"sample_id": sample_id, "outcome": "refused", "error_code": "scope"})


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _genuine_structural_attempt() -> DirectAPIReviewAttempt216:
    """A genuine structural-format attempt: 2xx bytes that fail extraction."""
    transport = FakeTransport([b'{"choices": "not-a-list"}'])
    return _reviewer(transport, zai="token").review("model_c", _SAMPLE)


def _genuine_judged_attempt() -> DirectAPIReviewAttempt216:
    transport = FakeTransport([_chat(_judged_content())])
    return _reviewer(transport, openrouter="token").review("model_a", _SAMPLE)


def _replace(attempt: DirectAPIReviewAttempt216, **fields: object) -> DirectAPIReviewAttempt216:
    """Mutate fields on a genuine attempt (dataclasses.replace)."""
    return dataclasses.replace(attempt, **fields)


# ---------------------------------------------------------------------------
# 1. Campaign-level invariant
# ---------------------------------------------------------------------------


class TestCampaignProvenanceInvariant:
    def test_direct_api_is_the_only_active_001k_reviewer_mode(self) -> None:
        assert frozenset({"direct_api_provenance"}) == ACTIVE_001K_PROVENANCE_MODES
        require_active_001k_provenance_mode("direct_api_provenance")  # no raise

    def test_superseded_modes_fail_closed_with_stable_errors(self) -> None:
        with pytest.raises(ValueError, match="campaign_001k_subscription_ui_superseded"):
            require_active_001k_provenance_mode("operator_attested_subscription_ui")
        with pytest.raises(ValueError, match="campaign_001k_machine_reviewer_superseded"):
            require_active_001k_provenance_mode("machine_executor_provenance")

    def test_future_mode_cannot_silently_become_active_001k_route(self) -> None:
        with pytest.raises(ValueError, match="campaign_001k_provenance_mode_not_active"):
            require_active_001k_provenance_mode("some_future_agent_provenance")


def _complete_direct_lane(monkeypatch, tmp_path: Path) -> tuple[Any, Any, Any]:
    """Complete one genuine direct-API 001k lane through the real runner.

    Uses the #206 round-6 lane scaffold (3 samples), a fake no-network
    transport, and a monkeypatched stage authority whose digests the direct
    authority is built from.  Returns (session, sampling, LaneFreeze).
    """
    import evals.calibration.api_reviewer_216 as api_module
    from evals.calibration.api_dev_review_216 import _authority
    from evals.calibration.api_dev_review_216 import _reviewer as _panel_reviewer
    from evals.calibration.model_lanes import freeze_lane
    from tests.test_calibration_206_round6 import _setup_lane

    # The lane reviewer must be the EXACT frozen direct-panel identity (the
    # runner's evidence is built from the panel route), not the generic
    # 206 test identity.
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path,
        campaign_id=CAMPAIGN_ID_001K,
        provenance_mode="direct_api_provenance",
        reviewer=_panel_reviewer("model_a"),
    )
    ids = sorted(sampling.sample_ids)

    class _Stage:
        stage = "dev"
        target_identity_digest = "b" * 64
        membership_digest = hashlib.sha256(json.dumps(ids).encode()).hexdigest()

        def require_capability(self) -> None:
            return None

    import evals.calibration.campaign_001k_stage_authority as stage_module

    monkeypatch.setattr(stage_module, "verify_stage_authority", lambda **_kw: _Stage())

    class _Transport:
        def post(self, *, url: str, headers: dict[str, str], body: bytes) -> HTTPResponseCapture:
            user = json.loads(json.loads(body)["messages"][1]["content"])
            sample_id = user["sample_id"]
            content = json.dumps(
                {
                    "sample_id": sample_id,
                    "outcome": "judged",
                    "judgment": {"fields": _FIELDS, "reviewer_confidence": "medium"},
                }
            )
            raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            return HTTPResponseCapture(200, {"x-request-id": "fake"}, raw, url)

    monkeypatch.setattr(api_module, "_CONCRETE_TRANSPORT_TYPE", _Transport)
    authority = _authority(
        session,
        target_digest=_Stage.target_identity_digest,
        membership_digest=_Stage.membership_digest,
    )
    runner = api_module.DirectReviewerRunner216(
        session, authority, credentials={"OPENROUTER_API_KEY": "fake"}
    )
    batch = session.emit_requests(packet, sampling=sampling, manifest_path=_manifest)
    for line in batch.read_text().splitlines():
        runner.review_request_line(line)
        runner.ingest_accepted(line, sampling=sampling)
    frozen = freeze_lane(
        protected_root=tmp_path,
        reviewer=session.reviewer,
        campaign_id=CAMPAIGN_ID_001K,
        sampling=sampling,
        source_packet_digest=session.source_packet_digest,
    )
    return session, sampling, frozen


# ---------------------------------------------------------------------------
# 2. Machine executor supersession
# ---------------------------------------------------------------------------


class TestMachineReviewerSuperseded:
    def test_run_machine_dev_review_never_creates_001k_evidence(self, tmp_path: Path) -> None:
        from evals.calibration.machine_dev_review_216 import (
            MACHINE_DEV_REVIEW_SUPERSEDED_ERROR,
            run_machine_dev_review,
        )

        with pytest.raises(ValueError, match=MACHINE_DEV_REVIEW_SUPERSEDED_ERROR):
            run_machine_dev_review(tmp_path, dry_run=True)
        # No lane state was ever created.
        assert not (tmp_path / "lanes").exists()

    def test_machine_cli_fails_closed_before_any_execution(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.calibration",
                "216-machine-dev-review",
                "--protected-dir",
                "/nonexistent-protected-root",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert result.returncode != 0
        assert "campaign_001k_machine_reviewer_superseded" in result.stderr

    def test_machine_provenance_cannot_freeze_or_reload_as_001k(self, tmp_path: Path) -> None:
        """An otherwise fully valid machine lane is rejected at freeze."""
        from tests.test_calibration_206_round6 import _setup_lane
        from tests.test_calibration_216_machine_reviewer import _authority, _runner, _valid_response

        session, sampling, packet, _manifest = _setup_lane(
            tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
        )
        lines = session.emit_requests(packet, sampling=sampling).read_text().splitlines()
        runner, _calls = _runner(
            session,
            _authority(
                session,
                target_identity_digest="b" * 64,
                membership_digest=hashlib.sha256(
                    json.dumps(sorted(json.loads(line)["sample_id"] for line in lines)).encode()
                ).hexdigest(),
            ),
            [_valid_response(json.loads(line)["sample_id"]) for line in lines],
        )
        for line in lines:
            runner.review_request_line(line)
            runner.ingest_accepted(line, sampling=sampling)

        from evals.calibration.model_lanes import freeze_lane

        with pytest.raises(ValueError, match="campaign_001k_machine_reviewer_superseded"):
            freeze_lane(
                protected_root=tmp_path,
                reviewer=session.reviewer,
                campaign_id=CAMPAIGN_ID_001K,
                sampling=sampling,
                source_packet_digest=session.source_packet_digest,
            )

    def test_direct_api_lane_still_freezes_and_reloads(self, monkeypatch, tmp_path: Path) -> None:
        """The ONE active mode keeps working end-to-end (freeze + reload)."""
        session, sampling, frozen = _complete_direct_lane(monkeypatch, tmp_path)
        from evals.calibration.model_lanes import _load_one_frozen_lane

        reloaded = _load_one_frozen_lane(
            tmp_path,
            session.reviewer,
            CAMPAIGN_ID_001K,
            sampling,
            session.source_packet_digest,
        )
        assert reloaded.sample_ids == frozen.sample_ids


# ---------------------------------------------------------------------------
# 3. Forged structural retry authorization (mutation attacks)
# ---------------------------------------------------------------------------


class TestForgedStructuralClassification:
    def test_mutated_structural_attempt_now_parsing_as_judgment_is_rejected(self) -> None:
        """Coherently mutate a genuine structural attempt so its retained
        raw response parses as a valid judgment — the stored structural
        classification must still be rejected."""
        base = _genuine_structural_attempt()
        judged_content = _judged_content()
        new_raw = _chat(judged_content)
        mutated = _replace(
            base,
            raw_response=new_raw,
            raw_response_sha256=_sha(new_raw),
            extracted_content=judged_content,
            extracted_content_sha256=_sha(judged_content.encode()),
        )
        with pytest.raises(ValueError, match="direct_api_structural_classification_not_derived"):
            verify_direct_api_attempt_216(mutated)

    def test_mutated_structural_attempt_now_parsing_as_refused_is_rejected(self) -> None:
        base = _genuine_structural_attempt()
        refused = _refused_content()
        new_raw = _chat(refused)
        mutated = _replace(
            base,
            raw_response=new_raw,
            raw_response_sha256=_sha(new_raw),
            extracted_content=refused,
            extracted_content_sha256=_sha(refused.encode()),
        )
        with pytest.raises(ValueError, match="direct_api_structural_classification_not_derived"):
            verify_direct_api_attempt_216(mutated)

    def test_terminal_judgment_cannot_survive_malformed_raw_bytes(self) -> None:
        """Inverse: a terminal judgment whose retained raw bytes mechanically
        fail extraction must fail closed."""
        base = _genuine_judged_attempt()
        broken_raw = b'{"choices": [{"message": {}}]}'  # content missing -> extraction fails
        mutated = _replace(
            base,
            raw_response=broken_raw,
            raw_response_sha256=_sha(broken_raw),
        )
        with pytest.raises(ValueError, match="direct_api_structural_classification_not_derived"):
            verify_direct_api_attempt_216(mutated)

    def test_terminal_judgment_cannot_survive_unparseable_content(self) -> None:
        """Terminal judgment over content that extracts but cannot re-parse."""
        base = _genuine_judged_attempt()
        bad_content = "not valid reviewer json"
        new_raw = _chat(bad_content)
        mutated = _replace(
            base,
            raw_response=new_raw,
            raw_response_sha256=_sha(new_raw),
            extracted_content=bad_content,
            extracted_content_sha256=_sha(bad_content.encode()),
        )
        with pytest.raises(ValueError, match="direct_api_structural_classification_not_derived"):
            verify_direct_api_attempt_216(mutated)

    def test_genuine_structural_attempt_still_verifies(self) -> None:
        verify_direct_api_attempt_216(_genuine_structural_attempt())

    def test_genuine_judged_attempt_still_verifies(self) -> None:
        verify_direct_api_attempt_216(_genuine_judged_attempt())


# ---------------------------------------------------------------------------
# 4. Request-identity digest binding
# ---------------------------------------------------------------------------


class TestRequestIdentityBinding:
    def test_mutation_of_request_identity_digest_alone_fails_closed(self) -> None:
        base = _genuine_judged_attempt()
        forged_digest = _sha(b"some-other-request-bytes")
        assert forged_digest != base.request_identity_digest
        mutated = _replace(base, request_identity_digest=forged_digest)
        with pytest.raises(ValueError, match="direct_api_request_identity_digest_mismatch"):
            verify_direct_api_attempt_216(mutated)

    def test_request_sha256_mismatch_also_breaks_identity_binding(self) -> None:
        base = _genuine_judged_attempt()
        forged = _sha(b"other")
        mutated = _replace(base, request_sha256=forged, request_identity_digest=forged)
        with pytest.raises(ValueError, match="direct_api_request_digest_mismatch"):
            verify_direct_api_attempt_216(mutated)


# ---------------------------------------------------------------------------
# 5. Provenance-mode substitution after direct lane creation
# ---------------------------------------------------------------------------


class TestProvenanceModeSubstitution:
    def test_lane_authority_flip_to_machine_mode_fails_at_freeze(
        self, monkeypatch, tmp_path
    ) -> None:
        """Create a genuine completed direct lane, then flip its authority
        bytes to the superseded machine mode: the freeze boundary must fail
        closed with the stable superseded error."""
        session, sampling, _frozen = _complete_direct_lane(monkeypatch, tmp_path)
        lane_json = tmp_path / "lanes" / session.reviewer.reviewer_slot / "lane.json"
        payload = json.loads(lane_json.read_text())
        assert payload["provenance_mode"] == "direct_api_provenance"
        payload["provenance_mode"] = "machine_executor_provenance"
        lane_json.write_text(json.dumps(payload, sort_keys=True))
        from evals.calibration.model_lanes import freeze_lane

        with pytest.raises(ValueError, match="campaign_001k_machine_reviewer_superseded"):
            freeze_lane(
                protected_root=tmp_path,
                reviewer=session.reviewer,
                campaign_id=CAMPAIGN_ID_001K,
                sampling=sampling,
                source_packet_digest=session.source_packet_digest,
            )

    def test_lane_authority_flip_to_subscription_mode_fails_at_freeze(
        self, monkeypatch, tmp_path
    ) -> None:
        session, sampling, _frozen = _complete_direct_lane(monkeypatch, tmp_path)
        lane_json = tmp_path / "lanes" / session.reviewer.reviewer_slot / "lane.json"
        payload = json.loads(lane_json.read_text())
        payload["provenance_mode"] = "operator_attested_subscription_ui"
        lane_json.write_text(json.dumps(payload, sort_keys=True))
        from evals.calibration.model_lanes import freeze_lane

        with pytest.raises(ValueError, match="campaign_001k_subscription_ui_superseded"):
            freeze_lane(
                protected_root=tmp_path,
                reviewer=session.reviewer,
                campaign_id=CAMPAIGN_ID_001K,
                sampling=sampling,
                source_packet_digest=session.source_packet_digest,
            )

    def test_unknown_provenance_mode_flip_fails_closed(self, tmp_path: Path) -> None:
        from evals.calibration.ingestion import LaneSession
        from tests.test_calibration_206_round6 import _setup_lane

        session, sampling, packet, _manifest = _setup_lane(
            tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="direct_api_provenance"
        )
        del session, packet, sampling
        lane_json = tmp_path / "lanes" / "model_a" / "lane.json"
        payload = json.loads(lane_json.read_text())
        payload["provenance_mode"] = "future_agent_mode"
        lane_json.write_text(json.dumps(payload, sort_keys=True))
        with pytest.raises(ValueError, match="lane_unknown_provenance_mode|provenance_mode"):
            LaneSession(tmp_path, "model_a")

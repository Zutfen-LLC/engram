"""Adversarial proofs for the #216 machine-orchestrated reviewer foundation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.calibration.campaign_001k import CAMPAIGN_ID_001K
from evals.calibration.ingestion import labeling_instructions_digest
from evals.calibration.machine_reviewer_216 import (
    CommandResult,
    MachineReviewerAuthority,
    MachineReviewerRunner,
    RetryExhaustedError,
)
from tests.test_calibration_206_round6 import _setup_lane


def _authority(session, **updates):
    values = {
        "campaign_id": CAMPAIGN_ID_001K,
        "reviewer": session.reviewer,
        "hermes_version": "0.9.0",
        "resolved_provider_identifier": "openai",
        "resolved_model_identifier": session.reviewer.provider_model_identifier,
        "auth_mechanism_class": "environment_bound_provider_credentials",
        "endpoint_routing": "https://provider.invalid/v1",
        "config_digest": session.reviewer.reviewer_config_digest,
        "prompt_digest": labeling_instructions_digest(),
        "source_packet_digest": session.source_packet_digest,
        "target_identity_digest": "b" * 64,
        "membership_digest": hashlib.sha256(
            json.dumps(sorted(session.authority.reviewer.model_dump().keys())).encode()
        ).hexdigest(),
        "generation_params": {"temperature": 0, "max_tokens": 1024},
        "mode": "machine_orchestrated",
        "reviewer_version": "machine-reviewer-216-v1",
    }
    values.update(updates)
    return MachineReviewerAuthority(**values)


def _runner(session, authority, outputs):
    calls = []

    def command_runner(argv: tuple[str, ...], *, cwd: Path) -> str:
        calls.append((argv, cwd))
        return outputs.pop(0)

    return MachineReviewerRunner(session, authority, command_runner=command_runner), calls


def _valid_response(sample_id: str) -> str:
    return json.dumps(
        {
            "sample_id": sample_id,
            "outcome": "judged",
            "judgment": {
                "fields": {
                    "expected_kind": "fact",
                    "retention_value": "retain",
                    "epistemic_state": "adequately_supported",
                    "consequence": "low",
                    "acceptable_abstention": "no",
                },
                "reviewer_confidence": "medium",
            },
        }
    )


def test_wrong_identity_is_rejected_before_any_command(tmp_path: Path) -> None:
    session, _sampling, _packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    with pytest.raises(ValueError, match="machine_reviewer_identity_mismatch"):
        _authority(session, resolved_model_identifier="other-model")


def test_slots_use_isolated_raw_paths_and_fresh_oneshot_commands(tmp_path: Path) -> None:
    session_a, _sampling, _packet, _manifest = _setup_lane(
        tmp_path / "a",
        slot="model_a",
        campaign_id=CAMPAIGN_ID_001K,
        provenance_mode="machine_executor_provenance",
    )
    session_b, _sampling_b, _packet_b, _manifest_b = _setup_lane(
        tmp_path / "b",
        slot="model_b",
        campaign_id=CAMPAIGN_ID_001K,
        provenance_mode="machine_executor_provenance",
    )
    requests_a = session_a.emit_requests(_packet, sampling=_sampling).read_text().splitlines()
    requests_b = session_b.emit_requests(_packet_b, sampling=_sampling_b).read_text().splitlines()
    sample_a = requests_a[0]
    sample_b = requests_b[0]
    sid_a = json.loads(sample_a)["sample_id"]
    sid_b = json.loads(sample_b)["sample_id"]
    runner_a, calls_a = _runner(session_a, _authority(session_a), [_valid_response(sid_a)])
    runner_b, calls_b = _runner(session_b, _authority(session_b), [_valid_response(sid_b)])
    attempt_a = runner_a.review_request_line(sample_a)
    attempt_b = runner_b.review_request_line(sample_b)
    assert attempt_a.raw_stdout_path != attempt_b.raw_stdout_path
    assert "lanes/model_a/" in attempt_a.raw_stdout_path
    assert "lanes/model_b/" in attempt_b.raw_stdout_path
    for calls in (calls_a, calls_b):
        argv, _cwd = calls[0]
        assert argv[:4] == ("hermes", "chat", "--oneshot", "-Q")
        assert "--safe-mode" not in argv
        assert "--provider" in argv and "-m" in argv and "--query-file" in argv


def test_invalid_format_retries_with_immutable_authority_and_raw_attempts(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    line = session.emit_requests(packet, sampling=sampling).read_text().splitlines()[0]
    sample_id = json.loads(line)["sample_id"]
    authority = _authority(session)
    runner, _calls = _runner(session, authority, ["not json", _valid_response(sample_id)])
    accepted = runner.review_request_line(line, max_format_attempts=2)
    assert accepted.accepted is True
    attempts = runner.load_attempts(sample_id)
    assert [attempt.sequence for attempt in attempts] == [1, 2]
    assert attempts[0].authority_digest == attempts[1].authority_digest
    assert attempts[0].authority_digest == authority.authority_digest()
    assert Path(attempts[0].raw_stdout_path).read_text() == "not json"
    assert Path(attempts[1].raw_stdout_path).read_text() == _valid_response(sample_id)


def test_first_valid_acceptance_cannot_be_replaced(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    line = session.emit_requests(packet, sampling=sampling).read_text().splitlines()[0]
    sample_id = json.loads(line)["sample_id"]
    runner, _calls = _runner(session, _authority(session), [_valid_response(sample_id)])
    first = runner.review_request_line(line)
    assert first.accepted is True
    with pytest.raises(ValueError, match="machine_reviewer_first_acceptance_exists"):
        runner.review_request_line(line)


def test_format_retry_exhaustion_is_explicit_and_preserves_every_attempt(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    line = session.emit_requests(packet, sampling=sampling).read_text().splitlines()[0]
    sample_id = json.loads(line)["sample_id"]
    runner, _calls = _runner(session, _authority(session), ["bad one", "bad two"])
    with pytest.raises(RetryExhaustedError, match="machine_reviewer_format_retry_exhausted"):
        runner.review_request_line(line, max_format_attempts=2)
    attempts = runner.load_attempts(sample_id)
    assert len(attempts) == 2
    assert not any(attempt.accepted for attempt in attempts)


def test_request_membership_guard_rejects_unemitted_or_wrong_slot_requests(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    line = json.loads(session.emit_requests(packet, sampling=sampling).read_text().splitlines()[0])
    line["sample_id"] = "sffffffffffffffffffffffff"
    runner, calls = _runner(session, _authority(session), ["should not run"])
    with pytest.raises(ValueError, match="machine_reviewer_request_membership_mismatch"):
        runner.review_request_line(json.dumps(line))
    assert calls == []


def test_non_216_campaign_authority_is_rejected(tmp_path: Path) -> None:
    session, _sampling, _packet, _manifest = _setup_lane(tmp_path)
    with pytest.raises(ValueError, match="machine_reviewer_campaign_not_216"):
        _authority(session, campaign_id="eng-calibration-001f")


def test_command_error_preserves_raw_bytes_and_exit_state(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    line = session.emit_requests(packet, sampling=sampling).read_text().splitlines()[0]
    raw = b"provider command failed\xff"

    def failed(_argv, *, cwd):
        now = datetime.now(UTC)
        return CommandResult(raw, 17, now, now)

    runner = MachineReviewerRunner(session, _authority(session), command_runner=failed)
    with pytest.raises(RetryExhaustedError):
        runner.review_request_line(line)
    attempt = runner.load_attempts(json.loads(line)["sample_id"])[0]
    assert attempt.exit_code == 17
    assert attempt.parse_error == "command_exit_17"
    assert Path(attempt.raw_stdout_path).read_bytes() == raw


def test_accepted_machine_record_reaches_real_freeze_verifier(tmp_path: Path, monkeypatch) -> None:
    """The accepted stdout is ingested through LaneSession, then the actual
    freeze path re-checks its machine-executor receipt and raw attempt bytes."""
    import evals.calibration.campaign_001k_stage_authority as stage_authority
    from evals.calibration.model_lanes import freeze_lane

    class _Stage:
        target_identity_digest = "b" * 64
        membership_digest = hashlib.sha256(
            json.dumps(sorted(("s1", "s2", "s3"))).encode()
        ).hexdigest()

    monkeypatch.setattr(stage_authority, "verify_stage_authority", lambda **_: _Stage())

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
                json.dumps(sorted(("s1", "s2", "s3"))).encode()
            ).hexdigest(),
        ),
        [_valid_response(json.loads(line)["sample_id"]) for line in lines],
    )
    for line in lines:
        runner.review_request_line(line)
        record = runner.ingest_accepted(line, sampling=sampling)
        assert record.execution.identity_source == "machine_executor_provenance"
    frozen = freeze_lane(
        protected_root=tmp_path,
        reviewer=session.reviewer,
        campaign_id=CAMPAIGN_ID_001K,
        sampling=sampling,
        source_packet_digest=session.source_packet_digest,
    )
    assert frozen.sample_ids == sampling.sample_ids
    # This is the final-ledger's composed provenance verifier, not a mock.
    from evals.calibration.model_lanes import load_lane_records
    from evals.calibration.raw_evidence import validate_lane_provenance_with_raw

    validate_lane_provenance_with_raw(
        frozen,
        load_lane_records(tmp_path, session.reviewer.reviewer_slot),
        campaign_id=CAMPAIGN_ID_001K,
        sampling=sampling,
        source_packet_digest=session.source_packet_digest,
        protected_root=tmp_path,
    )


def test_canonical_batch_is_one_process_and_contains_only_its_lane(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    batch = session.emit_requests(packet, sampling=sampling)
    lines = batch.read_text().splitlines()
    calls: list[Path] = []

    def batch_runner(argv, *, cwd):
        query_path = Path(argv[-1])
        calls.append(query_path)
        payload = json.loads(query_path.read_text())
        assert set(payload) == {"requests"}
        assert [item["case"]["sample_id"] for item in payload["requests"]] == [
            json.loads(line)["sample_id"] for line in lines
        ]
        now = datetime.now(UTC)
        return CommandResult(
            json.dumps(
                [json.loads(_valid_response(json.loads(line)["sample_id"])) for line in lines]
            ).encode(),
            0,
            now,
            now,
        )

    runner = MachineReviewerRunner(session, _authority(session), command_runner=batch_runner)
    attempts = runner.review_emitted_batch(batch)
    assert len(calls) == 1
    assert len(attempts) == len(lines)


def test_whole_batch_retry_retains_failed_batch_bytes_before_success(tmp_path: Path) -> None:
    session, sampling, packet, _manifest = _setup_lane(
        tmp_path, campaign_id=CAMPAIGN_ID_001K, provenance_mode="machine_executor_provenance"
    )
    batch = session.emit_requests(packet, sampling=sampling)
    lines = batch.read_text().splitlines()
    outputs = [
        b"not an array",
        json.dumps(
            [json.loads(_valid_response(json.loads(line)["sample_id"])) for line in lines]
        ).encode(),
    ]

    def runner(_argv, *, cwd):
        now = datetime.now(UTC)
        return CommandResult(outputs.pop(0), 0, now, now)

    machine = MachineReviewerRunner(session, _authority(session), command_runner=runner)
    attempts = machine.review_emitted_batch(batch, max_format_attempts=2)
    assert len(attempts) == len(lines)
    root = tmp_path / "lanes" / "model_a" / "machine-reviewer" / "batches" / "000001-000003"
    assert (root / "attempt-000001" / "stdout").read_bytes() == b"not an array"
    assert json.loads((root / "attempt-000001" / "attempt.json").read_text())["accepted"] is False
    assert json.loads((root / "attempt-000002" / "attempt.json").read_text())["accepted"] is True
    assert {attempt.sequence for attempt in attempts} == {2}


def test_216_exact_machine_families_do_not_relabel_sonnet_as_opus() -> None:
    from evals.calibration.consensus import CAMPAIGN_216_FAMILY_BY_SLOT, ReviewerIdentity

    identity = ReviewerIdentity(
        reviewer_slot="model_a",
        reviewer_family=CAMPAIGN_216_FAMILY_BY_SLOT["model_a"],
        campaign_id=CAMPAIGN_ID_001K,
        provider_model_identifier="anthropic/claude-sonnet-5",
        reviewer_config_digest="a" * 64,
        prompt_digest=labeling_instructions_digest(),
    )
    assert identity.reviewer_family == "claude-sonnet-5"


def test_216_machine_cli_dry_run_dispatches_without_provider_execution(monkeypatch, capsys) -> None:
    import sys

    import evals.calibration.__main__ as cli
    import evals.calibration.machine_dev_review_216 as campaign

    def dry_only(_root: Path, *, dry_run: bool, **_kwargs):
        assert dry_run is True
        return {"logical_cases": 102, "holdout": 0, "lanes": {}}

    monkeypatch.setattr(campaign, "run_machine_dev_review", dry_only)
    monkeypatch.setattr(
        sys,
        "argv",
        ["calibration", "216-machine-dev-review", "--protected-dir", "/never-read", "--dry-run"],
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"holdout": 0, "lanes": {}, "logical_cases": 102}


def test_machine_dev_dry_run_is_exactly_102_and_never_preflights_a_provider(
    tmp_path: Path, monkeypatch
) -> None:
    from evals.calibration.machine_dev_review_216 import (
        MACHINE_IDENTITIES_216,
        RuntimeIdentity216,
        run_machine_dev_review,
    )
    from tests.test_calibration_206_round6 import _sampling

    sampling = _sampling(tuple(f"s{i:03d}" for i in range(102)))
    (tmp_path / "dev-sampling-manifest.json").write_text(sampling.model_dump_json())
    blind = tmp_path / f"{CAMPAIGN_ID_001K}-dev-v1.blind.json"
    blind.write_bytes(b"frozen-dev-packet")
    (tmp_path / "reuse-manifest.json").write_text(json.dumps({"holdout_ids": ["s-holdout"]}))

    class Stage:
        stage = "dev"
        target_identity_digest = "a" * 64
        membership_digest = "b" * 64

        def require_capability(self) -> None:
            return None

    monkeypatch.setattr(
        "evals.calibration.machine_dev_review_216.verify_stage_authority", lambda **_: Stage()
    )
    calls: list[tuple[str, str]] = []

    def preflight(provider: str, model: str) -> RuntimeIdentity216:
        calls.append((provider, model))
        return RuntimeIdentity216("hermes-test", provider, model, "fake")

    report = run_machine_dev_review(tmp_path, preflight=preflight, dry_run=True)
    assert report["logical_cases"] == 102
    assert report["holdout"] == 0
    assert calls == [(value[0], value[1]) for value in MACHINE_IDENTITIES_216.values()]


def test_machine_dev_rejects_wrong_provider_during_preflight_before_lane_creation(
    tmp_path: Path, monkeypatch
) -> None:
    from evals.calibration.machine_dev_review_216 import RuntimeIdentity216, run_machine_dev_review
    from tests.test_calibration_206_round6 import _sampling

    sampling = _sampling(tuple(f"s{i:03d}" for i in range(102)))
    (tmp_path / "dev-sampling-manifest.json").write_text(sampling.model_dump_json())
    (tmp_path / f"{CAMPAIGN_ID_001K}-dev-v1.blind.json").write_bytes(b"frozen-dev-packet")
    (tmp_path / "reuse-manifest.json").write_text(json.dumps({"holdout_ids": []}))

    class Stage:
        stage = "dev"
        target_identity_digest = "a" * 64
        membership_digest = "b" * 64

        def require_capability(self) -> None:
            return None

    monkeypatch.setattr(
        "evals.calibration.machine_dev_review_216.verify_stage_authority", lambda **_: Stage()
    )
    with pytest.raises(ValueError, match="machine_reviewer_runtime_identity_mismatch"):
        run_machine_dev_review(
            tmp_path,
            dry_run=True,
            preflight=lambda _provider, model: RuntimeIdentity216(
                "hermes-test", "wrong", model, "fake"
            ),
        )
    assert not (tmp_path / "lanes").exists()

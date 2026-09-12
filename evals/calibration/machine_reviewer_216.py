"""Self-contained machine reviewer orchestration for #216.

This module deliberately executes no campaign by itself.  A caller supplies one
already-emitted ``LaneSession`` request and an injected command runner in tests.
It freezes machine execution authority before the first subprocess invocation,
uses a fresh Hermes oneshot process for every attempt, and retains every query,
raw stdout, and attempt record under that lane only.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from pydantic import Field, model_validator

from evals.admission.schema import Digest, Record
from evals.calibration.campaign_001k import CAMPAIGN_ID_001K
from evals.calibration.consensus import ModelReviewRecord, ReviewerIdentity, digest_of
from evals.calibration.ingestion import LaneSession, request_item_digest
from evals.calibration.review import write_protected_file
from evals.calibration.reviewer_instructions import parse_model_response

MACHINE_REVIEWER_AUTHORITY_SCHEMA: Literal[
    "engram-calibration-machine-reviewer-authority-216-v1"
] = "engram-calibration-machine-reviewer-authority-216-v1"
MACHINE_REVIEWER_ATTEMPT_SCHEMA: Literal["engram-calibration-machine-reviewer-attempt-216-v1"] = (
    "engram-calibration-machine-reviewer-attempt-216-v1"
)
MACHINE_REVIEWER_VERSION: Literal["machine-reviewer-216-v1"] = "machine-reviewer-216-v1"


@dataclass(frozen=True)
class CommandResult:
    stdout: bytes
    exit_code: int
    started_at: datetime
    ended_at: datetime


class CommandRunner(Protocol):
    def __call__(self, argv: tuple[str, ...], *, cwd: Path) -> CommandResult | str: ...


class RetryExhaustedError(ValueError):
    """All allowed format attempts were preserved but none parsed."""


class MachineReviewerAuthority(Record):
    """Frozen pre-output authority for exactly one #216 machine lane.

    The authority contains identifiers and mechanisms only: never a credential,
    token, or provider response.  It is persisted before command execution and
    every attempt carries its canonical digest.
    """

    authority_schema: Literal["engram-calibration-machine-reviewer-authority-216-v1"] = (
        MACHINE_REVIEWER_AUTHORITY_SCHEMA
    )
    campaign_id: str
    reviewer: ReviewerIdentity
    hermes_version: str = Field(min_length=1)
    resolved_provider_identifier: str = Field(min_length=1)
    resolved_model_identifier: str = Field(min_length=1)
    auth_mechanism_class: Literal[
        "environment_bound_provider_credentials", "local_authenticated_profile"
    ]
    endpoint_routing: str = Field(min_length=1)
    config_digest: Digest
    prompt_digest: Digest
    source_packet_digest: Digest
    target_identity_digest: Digest
    membership_digest: Digest
    generation_params: dict[str, Any]
    mode: Literal["machine_orchestrated"]
    reviewer_version: Literal["machine-reviewer-216-v1"]

    @model_validator(mode="after")
    def reviewer_binding(self) -> Self:
        if self.campaign_id != CAMPAIGN_ID_001K:
            raise ValueError("machine_reviewer_campaign_not_216")
        if self.resolved_model_identifier != self.reviewer.provider_model_identifier:
            raise ValueError("machine_reviewer_identity_mismatch")
        if self.config_digest != self.reviewer.reviewer_config_digest:
            raise ValueError("machine_reviewer_identity_mismatch")
        if self.prompt_digest != self.reviewer.prompt_digest:
            raise ValueError("machine_reviewer_identity_mismatch")
        return self

    def authority_digest(self) -> Digest:
        return digest_of(self.model_dump(mode="json"))


class MachineReviewerAttempt(Record):
    """One immutable stdout capture and format-parse outcome."""

    attempt_schema: Literal["engram-calibration-machine-reviewer-attempt-216-v1"] = (
        MACHINE_REVIEWER_ATTEMPT_SCHEMA
    )
    sample_id: str
    sequence: int = Field(ge=1)
    authority_digest: Digest
    request_item_digest: Digest
    query_path: str
    raw_stdout_path: str
    raw_stdout_digest: Digest
    started_at: datetime
    ended_at: datetime
    exit_code: int
    minimum_correction: str | None = None
    parse_status: Literal["accepted", "invalid_format"]
    parse_error: str | None = None
    accepted: bool
    batch_attempt_receipt_path: str | None = None
    batch_attempt_receipt_digest: Digest | None = None
    batch_stdout_path: str | None = None
    batch_stdout_digest: Digest | None = None
    extraction_index: int | None = Field(default=None, ge=0)
    canonical_element_digest: Digest | None = None

    @model_validator(mode="after")
    def attempt_contract(self) -> Self:
        if self.accepted != (self.parse_status == "accepted"):
            raise ValueError("machine_reviewer_attempt_acceptance_mismatch")
        if self.accepted and self.parse_error is not None:
            raise ValueError("machine_reviewer_accepted_attempt_has_parse_error")
        if not self.accepted and not self.parse_error:
            raise ValueError("machine_reviewer_invalid_attempt_requires_parse_error")
        if self.ended_at < self.started_at:
            raise ValueError("machine_reviewer_attempt_invalid_time_range")
        if self.sequence == 1 and self.minimum_correction is not None:
            raise ValueError("machine_reviewer_first_attempt_must_not_carry_correction")
        if self.sequence > 1 and not self.minimum_correction:
            raise ValueError("machine_reviewer_retry_requires_minimum_correction")
        batch = (
            self.batch_attempt_receipt_path,
            self.batch_attempt_receipt_digest,
            self.batch_stdout_path,
            self.batch_stdout_digest,
            self.extraction_index,
            self.canonical_element_digest,
        )
        if any(v is not None for v in batch) and any(v is None for v in batch):
            raise ValueError("machine_reviewer_batch_binding_incomplete")
        return self


class MachineReviewerRunner:
    """Execute one emitted lane request through a fresh process per attempt."""

    def __init__(
        self,
        session: LaneSession,
        authority: MachineReviewerAuthority,
        *,
        command_runner: CommandRunner | None = None,
    ) -> None:
        if authority.campaign_id != session.campaign_id:
            raise ValueError("machine_reviewer_campaign_mismatch")
        if authority.reviewer != session.reviewer:
            raise ValueError("machine_reviewer_identity_mismatch")
        if authority.source_packet_digest != session.source_packet_digest:
            raise ValueError("machine_reviewer_source_packet_mismatch")
        if session.authority.provenance_mode != "machine_executor_provenance":
            raise ValueError("machine_reviewer_requires_machine_executor_lane")
        # Re-validate serialized authority so a model_copy/model_construct
        # lookalike cannot bypass the frozen identity validator.
        MachineReviewerAuthority.model_validate(authority.model_dump(mode="json"))
        self.session = session
        self.authority = authority
        self.command_runner = command_runner or _subprocess_command_runner
        self.root = session.lane_root / "machine-reviewer"
        self._persist_authority()

    def _persist_authority(self) -> None:
        path = self.root / "authority.json"
        serialized = json.dumps(self.authority.model_dump(mode="json"), sort_keys=True)
        payload = (serialized + "\n").encode()
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("machine_reviewer_authority_already_frozen")
            return
        write_protected_file(path, payload)

    def _request_for_line(self, line: str) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("machine_reviewer_request_invalid_json") from exc
        if not isinstance(request, dict):
            raise ValueError("machine_reviewer_request_invalid_json")
        expected = {
            "campaign_id": self.session.campaign_id,
            "reviewer_slot": self.session.reviewer.reviewer_slot,
            "reviewer_family": self.session.reviewer.reviewer_family,
            "provider_model_identifier": self.session.reviewer.provider_model_identifier,
            "reviewer_config_digest": self.session.reviewer.reviewer_config_digest,
            "prompt_digest": self.session.reviewer.prompt_digest,
            "source_packet_digest": self.session.source_packet_digest,
        }
        if any(request.get(key) != value for key, value in expected.items()):
            raise ValueError("machine_reviewer_request_membership_mismatch")
        sample_id = request.get("sample_id")
        if not isinstance(sample_id, str):
            raise ValueError("machine_reviewer_request_membership_mismatch")
        digest = request_item_digest(request)
        matched = False
        for batch in self.session.lane_root.glob("lane-requests-*.jsonl"):
            for emitted in batch.read_text().splitlines():
                if emitted.strip() and request_item_digest(json.loads(emitted)) == digest:
                    matched = True
                    break
            if matched:
                break
        if not matched:
            raise ValueError("machine_reviewer_request_membership_mismatch")
        return request

    def _attempt_dir(self, sample_id: str) -> Path:
        return self.root / "attempts" / sample_id

    def load_attempts(self, sample_id: str) -> tuple[MachineReviewerAttempt, ...]:
        attempt_dir = self._attempt_dir(sample_id)
        if not attempt_dir.exists():
            return ()
        return tuple(
            MachineReviewerAttempt.model_validate(json.loads(path.read_text()))
            for path in sorted(attempt_dir.glob("attempt-*.json"))
            if path.name.count(".") == 1
        )

    def _accepted_path(self, sample_id: str) -> Path:
        return self.root / "accepted" / f"{sample_id}.json"

    def _command(self, query_path: Path) -> tuple[str, ...]:
        return (
            "hermes",
            "chat",
            "--oneshot",
            "-Q",
            "--provider",
            self.authority.resolved_provider_identifier,
            "-m",
            self.authority.resolved_model_identifier,
            "--query-file",
            str(query_path),
        )

    def review_request_line(
        self, line: str, *, max_format_attempts: int = 1
    ) -> MachineReviewerAttempt:
        """Capture attempts until the first valid response, never replacing it."""
        if max_format_attempts < 1:
            raise ValueError("machine_reviewer_invalid_retry_limit")
        request = self._request_for_line(line)
        sample_id = request["sample_id"]
        if self._accepted_path(sample_id).exists():
            raise ValueError("machine_reviewer_first_acceptance_exists")
        prior = self.load_attempts(sample_id)
        if prior:
            raise ValueError("machine_reviewer_attempt_history_exists")
        authority_digest = self.authority.authority_digest()
        request_digest = request_item_digest(request)
        correction = "Return only the required JSON object; do not add prose or markdown."
        for sequence in range(1, max_format_attempts + 1):
            attempt_dir = self._attempt_dir(sample_id)
            query_path = attempt_dir / f"attempt-{sequence:06d}.query.json"
            stdout_path = attempt_dir / f"attempt-{sequence:06d}.stdout"
            attempt_path = attempt_dir / f"attempt-{sequence:06d}.json"
            query_payload: dict[str, Any] = {
                "case": request["case"],
                "labeling_instructions": request["labeling_instructions"],
            }
            if sequence > 1:
                query_payload["minimum_correction"] = correction
            write_protected_file(query_path, json.dumps(query_payload, sort_keys=True).encode())
            result = self.command_runner(self._command(query_path), cwd=self.session.lane_root)
            if isinstance(result, str):  # legacy injected test runners only
                now = datetime.now(UTC)
                result = CommandResult(result.encode(), 0, now, now)
            raw = result.stdout
            write_protected_file(stdout_path, raw)
            parsed = parse_model_response(raw, expected_sample_id=sample_id)
            accepted = parsed.classification == "judged" and result.exit_code == 0
            error = (
                None
                if accepted
                else (
                    f"command_exit_{result.exit_code}"
                    if result.exit_code
                    else (parsed.error_code or "invalid_response_format")
                )
            )
            attempt = MachineReviewerAttempt(
                sample_id=sample_id,
                sequence=sequence,
                authority_digest=authority_digest,
                request_item_digest=request_digest,
                query_path=str(query_path),
                raw_stdout_path=str(stdout_path),
                raw_stdout_digest=hashlib.sha256(raw).hexdigest(),
                started_at=result.started_at,
                ended_at=result.ended_at,
                exit_code=result.exit_code,
                minimum_correction=correction if sequence > 1 else None,
                parse_status="accepted" if accepted else "invalid_format",
                parse_error=error,
                accepted=accepted,
            )
            serialized_attempt = json.dumps(attempt.model_dump(mode="json"), sort_keys=True)
            write_protected_file(attempt_path, (serialized_attempt + "\n").encode())
            if accepted:
                write_protected_file(
                    self._accepted_path(sample_id),
                    (json.dumps(attempt.model_dump(mode="json"), sort_keys=True) + "\n").encode(),
                )
                return attempt
        raise RetryExhaustedError("machine_reviewer_format_retry_exhausted")

    def review_emitted_batch(
        self, batch_path: Path, *, chunk_size: int = 102, max_format_attempts: int = 2
    ) -> tuple[MachineReviewerAttempt, ...]:
        """Run one canonical lane batch (or fixed-size ordered chunks).

        The query contains only canonical request projections from *this* lane.
        A batch result is a JSON array in the same order; it is split into
        per-sample raw records only after preserving the original batch bytes.
        """
        if chunk_size < 1 or max_format_attempts < 1:
            raise ValueError("machine_reviewer_invalid_chunk_size")
        if batch_path.parent != self.session.lane_root:
            raise ValueError("machine_reviewer_batch_outside_lane")
        lines = [line for line in batch_path.read_text().splitlines() if line]
        requests = [self._request_for_line(line) for line in lines]
        attempts: list[MachineReviewerAttempt] = []
        for offset in range(0, len(requests), chunk_size):
            chunk = requests[offset : offset + chunk_size]
            chunk_id = f"{offset + 1:06d}-{offset + len(chunk):06d}"
            root = self.root / "batches" / chunk_id
            payload: list[Any] | None = None
            accepted_sequence = 0
            accepted_query_path: Path | None = None
            accepted_result: CommandResult | None = None
            for sequence in range(1, max_format_attempts + 1):
                attempt_root = root / f"attempt-{sequence:06d}"
                query_path = attempt_root / "query.json"
                stdout_path = attempt_root / "stdout"
                query: dict[str, Any] = {
                    "requests": [
                        {
                            "case": request["case"],
                            "labeling_instructions": request["labeling_instructions"],
                        }
                        for request in chunk
                    ]
                }
                if sequence > 1:
                    query["minimum_correction"] = (
                        "Return only a JSON array with one required JSON object per request."
                    )
                write_protected_file(query_path, json.dumps(query, sort_keys=True).encode())
                result = self.command_runner(self._command(query_path), cwd=self.session.lane_root)
                if isinstance(result, str):
                    now = datetime.now(UTC)
                    result = CommandResult(result.encode(), 0, now, now)
                write_protected_file(stdout_path, result.stdout)
                try:
                    decoded = json.loads(result.stdout)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = None
                # Whole-chunk validation happens before any per-sample writes.
                valid = (
                    isinstance(decoded, list)
                    and len(decoded) == len(chunk)
                    and result.exit_code == 0
                )
                canonical_elements: list[bytes] = []
                if valid:
                    for request, response in zip(chunk, decoded, strict=True):
                        try:
                            element = json.dumps(
                                response, sort_keys=True, separators=(",", ":")
                            ).encode()
                            parsed = parse_model_response(
                                element, expected_sample_id=str(request["sample_id"])
                            )
                        except (TypeError, ValueError):
                            valid = False
                            break
                        if parsed.classification != "judged":
                            valid = False
                            break
                        canonical_elements.append(element)
                batch_attempt = {
                    "sequence": sequence,
                    "query_path": str(query_path),
                    "raw_stdout_path": str(stdout_path),
                    "raw_stdout_digest": hashlib.sha256(result.stdout).hexdigest(),
                    "exit_code": result.exit_code,
                    "accepted": valid,
                }
                write_protected_file(
                    attempt_root / "attempt.json",
                    (json.dumps(batch_attempt, sort_keys=True) + "\n").encode(),
                )
                if valid:
                    payload = decoded
                    accepted_sequence = sequence
                    accepted_query_path = query_path
                    accepted_result = result
                    break
            if payload is None:
                raise RetryExhaustedError("machine_reviewer_batch_invalid_format")
            assert accepted_query_path is not None and accepted_result is not None
            receipt_path = root / f"attempt-{accepted_sequence:06d}" / "attempt.json"
            receipt_bytes = receipt_path.read_bytes()
            for extraction_index, (request, _response) in enumerate(
                zip(chunk, payload, strict=True)
            ):
                sample_id = str(request["sample_id"])
                if self._accepted_path(sample_id).exists() or self.load_attempts(sample_id):
                    raise ValueError("machine_reviewer_attempt_history_exists")
                # This is a canonical extraction for parser verification only;
                # raw stdout remains the original batch process bytes.
                raw = canonical_elements[extraction_index]
                attempt_dir = self._attempt_dir(sample_id)
                sample_query = attempt_dir / f"attempt-{accepted_sequence:06d}.query.json"
                sample_record = attempt_dir / f"attempt-{accepted_sequence:06d}.json"
                write_protected_file(sample_query, accepted_query_path.read_bytes())
                attempt = MachineReviewerAttempt(
                    sample_id=sample_id,
                    sequence=accepted_sequence,
                    authority_digest=self.authority.authority_digest(),
                    request_item_digest=request_item_digest(request),
                    query_path=str(sample_query),
                    raw_stdout_path=str(root / f"attempt-{accepted_sequence:06d}" / "stdout"),
                    raw_stdout_digest=hashlib.sha256(accepted_result.stdout).hexdigest(),
                    started_at=accepted_result.started_at,
                    ended_at=accepted_result.ended_at,
                    exit_code=0,
                    minimum_correction=(
                        "Return only a JSON array with one required JSON object per request."
                        if accepted_sequence > 1
                        else None
                    ),
                    parse_status="accepted",
                    accepted=True,
                    batch_attempt_receipt_path=str(receipt_path),
                    batch_attempt_receipt_digest=hashlib.sha256(receipt_bytes).hexdigest(),
                    batch_stdout_path=str(root / f"attempt-{accepted_sequence:06d}" / "stdout"),
                    batch_stdout_digest=hashlib.sha256(accepted_result.stdout).hexdigest(),
                    extraction_index=extraction_index,
                    canonical_element_digest=hashlib.sha256(raw).hexdigest(),
                )
                encoded = (
                    json.dumps(attempt.model_dump(mode="json"), sort_keys=True) + "\n"
                ).encode()
                write_protected_file(sample_record, encoded)
                write_protected_file(self._accepted_path(sample_id), encoded)
                attempts.append(attempt)
        return tuple(attempts)

    def ingest_accepted(self, line: str, *, sampling: Any) -> ModelReviewRecord:
        """Convert one first accepted machine attempt into the real lane record.

        This intentionally uses ``LaneSession.ingest_response``: all existing
        request binding, raw evidence, freeze/load, and consensus verification
        remains authoritative rather than creating a parallel acceptance path.
        """
        from evals.calibration.consensus import ExecutionEvidence, ExecutionReceipt, digest_of

        request = self._request_for_line(line)
        sample_id = str(request["sample_id"])
        accepted_path = self._accepted_path(sample_id)
        if not accepted_path.is_file():
            raise ValueError("machine_reviewer_accepted_attempt_missing")
        attempt = MachineReviewerAttempt.model_validate(json.loads(accepted_path.read_text()))
        if not attempt.accepted or attempt.request_item_digest != request_item_digest(request):
            raise ValueError("machine_reviewer_accepted_attempt_mismatch")
        if attempt.authority_digest != self.authority.authority_digest():
            raise ValueError("machine_reviewer_accepted_authority_mismatch")
        raw_path = Path(attempt.raw_stdout_path)
        if raw_path.parent != self._attempt_dir(sample_id) or not raw_path.is_file():
            raise ValueError("machine_reviewer_accepted_raw_missing")
        raw = raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != attempt.raw_stdout_digest:
            raise ValueError("machine_reviewer_accepted_raw_digest_mismatch")
        if attempt.exit_code != 0 or (
            attempt.extraction_index is None
            and parse_model_response(raw, expected_sample_id=sample_id).classification != "judged"
        ):
            raise ValueError("machine_reviewer_accepted_attempt_not_valid")
        response_raw = raw
        if attempt.extraction_index is not None:
            if attempt.batch_stdout_digest != hashlib.sha256(raw).hexdigest():
                raise ValueError("machine_reviewer_batch_stdout_digest_mismatch")
            decoded = json.loads(raw)
            if not isinstance(decoded, list) or attempt.extraction_index >= len(decoded):
                raise ValueError("machine_reviewer_batch_extraction_invalid")
            response_raw = json.dumps(
                decoded[attempt.extraction_index], sort_keys=True, separators=(",", ":")
            ).encode()
            if hashlib.sha256(response_raw).hexdigest() != attempt.canonical_element_digest or (
                parse_model_response(response_raw, expected_sample_id=sample_id).classification
                != "judged"
            ):
                raise ValueError("machine_reviewer_batch_extraction_invalid")
        generation = next(
            int(batch.stem.rsplit("-", 1)[1])
            for batch in self.session.lane_root.glob("lane-requests-*.jsonl")
            if any(
                request_item_digest(json.loads(item)) == attempt.request_item_digest
                for item in batch.read_text().splitlines()
                if item
            )
        )
        proof = {
            "authority_digest": attempt.authority_digest,
            "attempt_digest": digest_of(attempt.model_dump(mode="json")),
            "raw_stdout_digest": attempt.raw_stdout_digest,
            "exit_code": attempt.exit_code,
            "batch_attempt_receipt_digest": attempt.batch_attempt_receipt_digest,
            "batch_stdout_digest": attempt.batch_stdout_digest,
            "extraction_index": attempt.extraction_index,
            "canonical_element_digest": attempt.canonical_element_digest,
        }
        evidence = ExecutionEvidence(
            campaign_id=self.session.campaign_id,
            actual_reviewer_slot=self.session.reviewer.reviewer_slot,
            actual_reviewer_family=self.session.reviewer.reviewer_family,
            actual_provider_model_identifier=self.authority.resolved_model_identifier,
            actual_configuration_digest=self.authority.config_digest,
            actual_prompt_digest=self.authority.prompt_digest,
            request_generation=generation,
            request_item_digest=attempt.request_item_digest,
            executed_at=attempt.ended_at,
            executor_status="completed",
            executor_identity=f"hermes:{self.authority.hermes_version}",
            identity_source="machine_executor_provenance",
            machine_executor_provenance=proof,
        )
        return self.session.ingest_response(
            {
                "sample_id": sample_id,
                "raw_response": response_raw.decode("utf-8"),
                "execution": ExecutionReceipt.from_evidence(evidence).model_dump(mode="json"),
            },
            sampling=sampling,
        )


def _subprocess_command_runner(argv: tuple[str, ...], *, cwd: Path) -> CommandResult:
    """Real runner retained for operators; tests must inject a fake runner."""
    started_at = datetime.now(UTC)
    completed = subprocess.run(argv, cwd=cwd, check=False, capture_output=True)
    ended_at = datetime.now(UTC)
    # stdout is retained byte-for-byte even when the CLI exits nonzero.
    return CommandResult(completed.stdout, completed.returncode, started_at, ended_at)

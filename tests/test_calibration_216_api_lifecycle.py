"""Synthetic 102×3 direct-provider lifecycle proof for #216 (no network)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evals.calibration.api_dev_review_216 import run_api_dev_review
from evals.calibration.api_reviewer_216 import HTTPResponseCapture, TransportFailure
from evals.calibration.consensus import CONSENSUS_PROTOCOL_VERSION
from evals.calibration.model_lanes import NeutralModelPacket, write_neutral_packet
from evals.calibration.review import BlindPacket, _packet_file_payload, write_protected_file
from tests.test_calibration_206_helpers import build_frame_rows
from tests.test_calibration_216 import _FRESH_DEV_102, _canonical_stage_root

_FIELDS = {
    "expected_kind": "fact",
    "retention_value": "retain",
    "epistemic_state": "adequately_supported",
    "consequence": "low",
    "acceptable_abstention": "no",
}


def _response(sample_id: str, *, refused: bool = False, malformed: bool = False) -> bytes:
    if malformed:
        content = "not-json"
    elif refused:
        content = json.dumps({"sample_id": sample_id, "outcome": "refused", "error_code": "scope"})
    else:
        content = json.dumps(
            {
                "sample_id": sample_id,
                "outcome": "judged",
                "judgment": {"fields": _FIELDS, "reviewer_confidence": "medium"},
            }
        )
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class _SyntheticTransport:
    """One fake direct transport shared by all lanes; no provider calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._attempts: dict[tuple[str, str], int] = {}

    def post(self, *, url: str, headers: dict[str, str], body: bytes) -> HTTPResponseCapture:
        payload = json.loads(body)
        user = json.loads(payload["messages"][1]["content"])
        sample_id = user["sample_id"]
        model = payload["model"]
        key = (model, sample_id)
        number = self._attempts.get(key, 0) + 1
        self._attempts[key] = number
        self.calls.append((model, sample_id, body))
        if model == "openai/gpt-5.6-terra" and sample_id == _FRESH_DEV_102[1] and number == 1:
            raise TransportFailure("synthetic_transport_failure")
        raw = _response(
            sample_id,
            refused=(model == "glm-5.3" and sample_id in _FRESH_DEV_102[2:4]),
            malformed=(
                model == "anthropic/claude-sonnet-5"
                and sample_id == _FRESH_DEV_102[0]
                and number == 1
            ),
        )
        return HTTPResponseCapture(
            200, {"x-request-id": f"fake-{model}-{sample_id}-{number}"}, raw, url
        )


def _prepare_canonical_synthetic_root(root: Path) -> None:
    sampling, _unused = _canonical_stage_root(root)
    from evals.calibration.freeze import protected_frame_digest

    frame_rows = build_frame_rows(tuple(_FRESH_DEV_102))
    sampling = sampling.model_copy(
        update={
            "frame_digest": protected_frame_digest(frame_rows),
            "sample_hashes": tuple(row.content_hash for row in frame_rows),
        }
    )
    (root / "dev-sampling-manifest.json").write_text(json.dumps(sampling.model_dump(mode="json")))
    cases = [
        {
            "sample_id": sid,
            "content": f"synthetic content {sid}",
            "governed_kind": "fact",
            "source_type": "manual",
            "review_status": "active",
            "assertion_mode": "unknown",
            "origin": "unknown",
            "risk": "unknown",
            "evidence_state": "unknown",
            "age_days": 1,
            "age_bucket": "lt_7d",
            "input_size_bucket": "small",
        }
        for sid in _FRESH_DEV_102
    ]
    blind = BlindPacket(
        packet_id="eng-calibration-001k-dev-v1",
        sampling_manifest_digest=sampling.manifest_digest(),
        guide_version="engram-calibration-guide-157-v1",
        reviewer_hint="blind",
        cases=cases,
    )
    blind_bytes = _packet_file_payload(blind)
    blind_path = root / "eng-calibration-001k-dev-v1.blind.json"
    blind_path.unlink()
    write_protected_file(blind_path, blind_bytes)
    neutral = NeutralModelPacket.from_blind(blind, protocol_version=CONSENSUS_PROTOCOL_VERSION)
    write_neutral_packet(neutral, root)
    assert neutral.source_packet_digest == hashlib.sha256(blind_bytes).hexdigest()


def test_direct_api_102x3_lifecycle_uses_canonical_lanes_and_persists_retries(
    tmp_path: Path,
) -> None:
    _prepare_canonical_synthetic_root(tmp_path)
    transport = _SyntheticTransport()

    report = run_api_dev_review(
        tmp_path,
        transport=transport,
        credentials={"OPENROUTER_API_KEY": "fake", "ZAI_API_KEY": "fake"},
    )

    assert report["logical_cases"] == 102
    assert report["reviewer_lanes"] == 3
    assert report["planned_logical_calls"] == 306
    assert report["transmitted_requests"] == 306
    assert len(transport.calls) == 308
    assert len({(model, sid) for model, sid, _body in transport.calls}) == 306

    from evals.calibration.api_dev_review_216 import _reviewer
    from evals.calibration.freeze import SamplingManifest
    from evals.calibration.model_lanes import load_frozen_lanes, records_by_lane_from_files

    sampling = SamplingManifest.model_validate(
        json.loads((tmp_path / "dev-sampling-manifest.json").read_text())
    )
    source_packet_digest = hashlib.sha256(
        (tmp_path / "eng-calibration-001k-dev-v1.blind.json").read_bytes()
    ).hexdigest()
    reviewers = {slot: _reviewer(slot) for slot in ("model_a", "model_b", "model_c")}
    lanes = load_frozen_lanes(
        tmp_path,
        campaign_id="eng-calibration-001k",
        sampling=sampling,
        source_packet_digest=source_packet_digest,
        reviewers=reviewers,
    )
    records = records_by_lane_from_files(tmp_path)
    assert len(lanes) == 3
    assert all(tuple(lane.sample_ids) == tuple(_FRESH_DEV_102) for lane in lanes)
    assert {slot: len(by_sample) for slot, by_sample in records.items()} == {
        "model_a": 102,
        "model_b": 102,
        "model_c": 102,
    }
    holdout_ids = set(json.loads((tmp_path / "reuse-manifest.json").read_text())["holdout_ids"])
    assert not set(_FRESH_DEV_102) & holdout_ids

    from evals.calibration.consensus import classify_case, select_audit_sample_with_coverage
    from evals.calibration.human_queue import (
        HumanQueueJudgment,
        HumanQueueManifest,
        QueueEntry,
        record_final_resolution,
        reveal_model_votes,
        save_initial_judgment,
        write_queue,
    )
    from evals.calibration.ledger import verify_consensus_ledger

    frame_rows = {row.sample_id: row for row in build_frame_rows(tuple(_FRESH_DEV_102))}
    classifications = {
        sid: classify_case({slot: records[slot][sid] for slot in records}) for sid in _FRESH_DEV_102
    }
    audit_ids = set(
        select_audit_sample_with_coverage(
            [sid for sid, result in classifications.items() if result["consensus"]], frame_rows
        ).selected
    )
    entries = [
        QueueEntry(
            sample_id=sid,
            reasons=tuple(sorted((*classifications[sid]["escalation_reasons"], "audit_selected"))),
            audit_only=not classifications[sid]["escalation_reasons"],
        )
        for sid in _FRESH_DEV_102
        if classifications[sid]["escalation_reasons"] or sid in audit_ids
    ]
    queue_dir = tmp_path / "queue"
    write_queue(
        HumanQueueManifest(
            protocol_version=CONSENSUS_PROTOCOL_VERSION,
            campaign_id="eng-calibration-001k",
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest=source_packet_digest,
            entries=tuple(entries),
        ),
        queue_dir,
    )
    lane_digests = tuple(lane.lane_digest() for lane in lanes)
    for entry in entries:
        sid = entry.sample_id
        save_initial_judgment(
            HumanQueueJudgment(
                protocol_version=CONSENSUS_PROTOCOL_VERSION,
                campaign_id="eng-calibration-001k",
                sampling_manifest_digest=sampling.manifest_digest(),
                source_packet_digest=source_packet_digest,
                sample_id=sid,
                adjudicator_ref="synthetic-human",
                queue_reasons=entry.reasons,
                audit_selected="audit_selected" in entry.reasons,
                initial_critical=dict(_FIELDS),
                initial_confidence="high",
                initial_captured_at="2026-09-12T00:00:00+00:00",
            ),
            queue_dir,
        )
        current = {slot: records[slot][sid] for slot in records}
        reveal_model_votes(
            queue_dir,
            sid,
            current_records_by_slot=current,
            lane_digests=lane_digests,
            campaign_id="eng-calibration-001k",
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest=source_packet_digest,
        )
        record_final_resolution(
            queue_dir,
            sid,
            final_critical=dict(_FIELDS),
            final_confidence="high",
            current_records_by_slot=current,
            lane_digests=lane_digests,
            campaign_id="eng-calibration-001k",
            sampling_manifest_digest=sampling.manifest_digest(),
            source_packet_digest=source_packet_digest,
        )
    ledger = verify_consensus_ledger(
        campaign_id="eng-calibration-001k",
        sampling=sampling,
        source_packet_digest=source_packet_digest,
        lanes=lanes,
        records_by_lane=records,
        queue_dir=queue_dir,
        frame_rows=frame_rows,
        protected_root=tmp_path,
    )
    assert len(ledger.ledger.wrappers) == 102
    assert {wrapper.sample_id for wrapper in ledger.ledger.wrappers} == set(_FRESH_DEV_102)
    assert any(wrapper.entered_human_queue for wrapper in ledger.ledger.wrappers)

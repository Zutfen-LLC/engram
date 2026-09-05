"""Contract tests for the pre-adjudication blind-review handoff."""

from __future__ import annotations

import ast
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evals.admission.dataset import Dataset, Sample, build_dataset, data_digest
from evals.admission.policy import PolicyInput
from evals.admission.schema import Sampling

ROOT = Path(__file__).resolve().parents[1]


def _sample(
    name: str,
    *,
    source_type: str = "extraction",
    kind: str = "fact",
    created_at: datetime | None = None,
    receipt: dict[str, object] | None = None,
    retention_disposition: str | None = None,
    retention_evidence_at: datetime | None = None,
    conflict: str | None = None,
    dispute: bool = False,
    kind_auto_promote: bool = True,
) -> Sample:
    created_at = created_at or datetime(2026, 9, 1, tzinfo=UTC)
    sample_id = hashlib.sha256(name.encode()).hexdigest()
    policy = PolicyInput.model_validate(
        {
            "sample_id": sample_id,
            "content_hash": "sha256:" + hashlib.sha256((name + " content").encode()).hexdigest(),
            "source_type": source_type,
            "kind": kind,
            "review_status": "proposed",
            "created_at": created_at,
            "memory_confidence": 0.4,
            "source_confidence_prior": 0.4,
            "retention_confidence": 0.4 if receipt else None,
            "retention_disposition": retention_disposition,
            "retention_evidence_at": retention_evidence_at or (created_at if receipt else None),
            "conflict_resolution_status": conflict,
            "live": True,
            "superseded": False,
            "kind_enabled": True,
            "kind_auto_promote": kind_auto_promote,
            "external_dispute": dispute,
            "external_noise": False,
            "receipt": receipt,
            "job_state": "unknown",
            "recalled": "unknown",
        }
    )
    return Sample(sample_id=sample_id, content=None, policy_input=policy, label=None)


@pytest.fixture
def snapshot() -> Dataset:
    at = datetime(2026, 9, 5, tzinfo=UTC)
    samples = tuple(
        [_sample(f"common-{i}", created_at=at - timedelta(days=i % 4)) for i in range(80)]
        + [
            _sample(
                "migration-doctrine",
                source_type="migration",
                kind="doctrine",
                kind_auto_promote=False,
            ),
            _sample(
                "manual-preference",
                source_type="manual",
                kind="preference",
                kind_auto_promote=False,
            ),
            _sample("sync-observation", source_type="sync_turn", kind="observation"),
            _sample(
                "session-invariant",
                source_type="session_end",
                kind="invariant",
                kind_auto_promote=False,
            ),
            _sample("pre-compress-procedure", source_type="pre_compress", kind="procedure"),
            _sample("conflict", conflict="unresolved"),
            _sample("dispute", dispute=True),
            _sample("retention-context", retention_disposition="retain"),
            _sample(
                "cooling-window",
                created_at=at - timedelta(hours=2),
                retention_evidence_at=at - timedelta(hours=1),
            ),
        ]
    )
    config = Dataset.model_validate_json(
        (ROOT / "evals/admission/contract-v1.json").read_bytes()
    ).config
    assert config is not None
    sampling = Sampling(
        selection_method="census",
        selection_seed="source-snapshot",
        strata=("source_type",),
        per_stratum=1,
    )
    return build_dataset(
        samples,
        config=config,
        at=at,
        code_sha="0" * 40,
        dataset_id="dogfood-admission",
        dataset_version="test-snapshot",
        privacy="private_dogfood",
        sampling=sampling,
        population_count=len(samples),
        counts=(),
    )


def test_selection_is_deterministic_order_independent_and_covers_rare_strata(snapshot):
    from evals.admission.blind_review import SelectionDefinition, select_tranche

    definition = SelectionDefinition(selection_seed="eng-calibration-001b-v1", target_count=20)
    first = select_tranche(snapshot, definition, code_sha="1" * 40)
    reversed_snapshot = snapshot.model_copy(
        update={
            "samples": tuple(reversed(snapshot.samples)),
            "manifest": snapshot.manifest.model_copy(
                update={
                    "sample_ids": tuple(s.sample_id for s in reversed(snapshot.samples)),
                    "sample_content_hashes": tuple(
                        s.policy_input.content_hash for s in reversed(snapshot.samples)
                    ),
                    "data_digest": data_digest(
                        tuple(reversed(snapshot.samples)), snapshot.config, snapshot.evaluation_at
                    ),
                }
            ),
        }
    )
    second = select_tranche(reversed_snapshot, definition, code_sha="1" * 40)
    assert first.review_case_ids == second.review_case_ids
    assert first.sample_ids == second.sample_ids
    assert len(first.sample_ids) == 20
    assert first.coverage["source_type"]["migration"] == 1
    assert first.coverage["kind"]["doctrine"] == 1
    assert first.coverage["governance"]["conflict"] == 1
    assert first.coverage["governance"]["dispute"] == 1
    assert first.coverage["temporal_evidence"]["cooling_window"] == 1
    assert first.code_sha == "1" * 40


def test_packet_is_blind_has_no_raw_uuid_or_policy_derived_answer_and_redacts_secret(snapshot):
    from evals.admission.blind_review import SelectionDefinition, build_packet, select_tranche

    selection = select_tranche(
        snapshot, SelectionDefinition(selection_seed="seed", target_count=8), code_sha="2" * 40
    )
    content = {
        sample_id: f"Ordinary content for {sample_id[:8]}." for sample_id in selection.sample_ids
    }
    secret_id = selection.sample_ids[0]
    uuid_id = selection.sample_ids[1]
    content[secret_id] = "password=supersecret99"
    content[uuid_id] = "production item 00000000-0000-0000-0000-000000000001"
    packet = build_packet(snapshot, selection, content)
    rendered = json.dumps(packet, sort_keys=True)
    forbidden = (
        "current_policy_version",
        "current_selected_lane",
        "would_promote",
        "blocker_codes",
        "readiness_state",
        "terminal_under_current_policy",
        "current_job_state",
        "promotion_score",
        "threshold",
        "candidate_policy",
    )
    assert all(field not in rendered for field in forbidden)
    assert "supersecret99" not in rendered
    assert "[REDACTED: secret detector]" in rendered
    assert not any(str(value) in rendered for value in (uuid.UUID(int=1), uuid.UUID(int=2)))
    assert all(case["review_case_id"].startswith("rvw_") for case in packet["cases"])
    assert all("sample_id" not in case and "content_hash" not in case for case in packet["cases"])


def test_packet_membership_requires_hmac_identity_correspondence(snapshot):
    from evals.admission.blind_review import SelectionDefinition, build_packet, select_tranche

    selection = select_tranche(
        snapshot, SelectionDefinition(selection_seed="seed", target_count=5), code_sha="3" * 40
    )
    content = {sample_id: "safe proposition" for sample_id in selection.sample_ids[:-1]}
    with pytest.raises(ValueError, match="content_membership_mismatch"):
        build_packet(snapshot, selection, content)


def test_empty_resumable_state_has_no_reviewer_or_policy_values(snapshot):
    from evals.admission.blind_review import SelectionDefinition, build_review_state, select_tranche

    selection = select_tranche(
        snapshot, SelectionDefinition(selection_seed="seed", target_count=5), code_sha="4" * 40
    )
    state = build_review_state(selection)
    assert len(state["cases"]) == 5
    assert all(case["reviewer_a"] is None for case in state["cases"])
    assert all(case["reviewer_a_frozen_at"] is None for case in state["cases"])
    assert all(case["reviewer_b_required"] is None for case in state["cases"])
    assert all(case["policy_reveal"] is None for case in state["cases"])


def test_production_cannot_import_blind_review_tooling():
    for path in (ROOT / "engram").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("evals.admission.blind_review")
            elif isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith("evals.admission.blind_review")
                    for alias in node.names
                )


def test_private_artifacts_cannot_be_written_inside_repository(tmp_path):
    from evals.admission.blind_review import _secure_write

    with pytest.raises(ValueError, match="private_output_must_be_outside_repository"):
        _secure_write(ROOT / "private-packet.json", "never written")
    destination = tmp_path / "private-packet.json"
    _secure_write(destination, "private")
    assert destination.read_text() == "private"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_hmac_mapping_is_exact_and_not_content_matching():
    from evals.admission.blind_review import hmac_sample_id, resolve_content_rows

    key = bytes.fromhex("11" * 32)
    selected_uuid = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    other_uuid = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    selected_sample_id = hmac_sample_id(key, selected_uuid)
    rows = [
        {"id": selected_uuid, "content_hash": "sha256:" + "a" * 64, "content": "same text"},
        {"id": other_uuid, "content_hash": "sha256:" + "b" * 64, "content": "same text"},
    ]
    resolved = resolve_content_rows(rows, key, {selected_sample_id})
    assert resolved == {selected_sample_id: "same text"}
    with pytest.raises(ValueError, match="snapshot_content_hash_mismatch"):
        resolve_content_rows(
            rows,
            key,
            {selected_sample_id},
            expected_content_hashes={selected_sample_id: "sha256:" + "f" * 64},
        )
    with pytest.raises(ValueError, match="content_membership_mismatch"):
        resolve_content_rows(rows, key, {selected_sample_id, "f" * 64})

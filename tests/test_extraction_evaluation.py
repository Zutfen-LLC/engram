"""Extraction golden-set coverage and metric error sensitivity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engram.extraction_schema import ExtractRequest
from evals.extraction.run import aggregate, score_case


def test_golden_contract_coverage():
    golden = json.loads(Path("evals/extraction/golden-v1.json").read_text())
    assert golden["schema_version"] == "engram.extraction-golden.v1"
    ids = {case["id"] for case in golden["cases"]}
    assert len(ids) == len(golden["cases"]) == 19
    assert {
        "162d_ordinary_kind",
        "secret",
        "tool_interpretation",
        "unknown",
        "paraphrase",
        "no_memory",
        "correction",
        "pronouns",
    } <= ids
    for case in golden["cases"]:
        ExtractRequest(messages=case["messages"])
        for target in case["expected"]:
            assert not {"risk", "consequence", "admission"} & target.keys()


def test_metrics_count_false_positive_false_negative_and_attribution_error():
    case = {
        "id": "metric-proof",
        "messages": [{"message_id": "u", "content": "I prefer light mode."}],
        "expected": [
            {
                "match_all": ["light mode"],
                "kind": "preference",
                "role": "user",
                "assertion_mode": "direct_statement",
                "retention": "retain",
                "evidence_ids": ["u"],
                "cue_types": [],
            },
            {
                "match_all": ["missing preference"],
                "kind": "preference",
                "role": "user",
                "assertion_mode": "direct_statement",
                "retention": "retain",
                "evidence_ids": ["u"],
                "cue_types": [],
            },
        ],
    }
    candidate = {
        "content": "The user prefers light mode.",
        "outcome": "preview",
        "suggested_kind": "fact",
        "asserting_role": "assistant",
        "assertion_mode": "inference",
        "retention_disposition": "transient",
        "source_cues": [],
        "evidence": [{"message_id": "u", "start": 0, "end": 20}],
    }
    result = score_case(
        case,
        {
            "receipt": {
                "candidates": [
                    candidate,
                    {**candidate, "content": "An unsupported claim."},
                ]
            }
        },
        200,
    )
    metrics = aggregate([result])
    assert metrics["candidate_precision"] == metrics["candidate_recall"] == 0.5
    assert metrics["attribution_accuracy"] == metrics["kind_accuracy"] == 0
    assert metrics["retention_label_accuracy"] == 0
    assert metrics["reported_cost_usd"] is None


def test_recorded_live_evaluation_has_reproducible_metrics_and_receipt_hashes():
    import hashlib

    from engram.extraction import digest
    from engram.extraction_schema import PROMPT_VERSION, ExtractResponse

    golden_bytes = Path("evals/extraction/golden-v1.json").read_bytes()
    cases = {case["id"]: case for case in json.loads(golden_bytes)["cases"]}
    report = json.loads(Path("evals/extraction/live-v1.json").read_text())
    assert report["golden_sha256"] == hashlib.sha256(golden_bytes).hexdigest()
    assert report["prompt_version"] == PROMPT_VERSION
    recomputed = []
    for row in report["cases"]:
        recomputed.append(score_case(cases[row["case_id"]], row["response"], row["http_status"]))
        if row["http_status"] == 200:
            response = ExtractResponse.model_validate(row["response"])
            assert response.receipt_hash == digest(row["response"]["receipt"])
            assert response.receipt.prompt_version == PROMPT_VERSION
            for candidate in response.receipt.candidates:
                assert candidate.evidence_root == response.receipt.evidence_root
    assert report["metrics"] == pytest.approx(aggregate(recomputed), rel=1e-12, abs=1e-15)

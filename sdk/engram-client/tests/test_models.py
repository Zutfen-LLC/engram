"""Client-side model validation tests for the sensitivity enum.

The SDK's ``RememberRequest`` mirrors the server's Pydantic model so callers
get a validation error locally instead of a round trip to the API. The
product vocabulary is ``normal|sensitive|restricted`` — ``confidential`` is
not, and never was, a value the database accepts.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engram_client.models import ClassifyResponse, RecallResponse, RememberRequest


def test_remember_request_accepts_restricted() -> None:
    req = RememberRequest(content="secret plans", sensitivity="restricted")
    assert req.sensitivity == "restricted"


def test_remember_request_rejects_confidential() -> None:
    with pytest.raises(ValidationError):
        RememberRequest(content="secret plans", sensitivity="confidential")


def test_remember_request_accepts_session_end() -> None:
    req = RememberRequest(content="session summary", source_type="session_end")
    assert req.source_type == "session_end"


def test_remember_request_visibility_defaults_to_none() -> None:
    """ENG-SCOPE-001: the SDK default is None — the server derives the safe
    default (private with no workspace, workspace-shared with one)."""
    req = RememberRequest(content="bare fact")
    assert req.visibility is None


def test_remember_request_omits_none_visibility_from_serialized_json() -> None:
    """model_dump(exclude_none=True) — the pattern EngramClient.remember uses
    to serialize — omits visibility entirely when it's None, so the server
    sees a genuinely-absent field rather than an explicit null."""
    req = RememberRequest(content="bare fact")
    payload = req.model_dump(mode="json", exclude_none=True)
    assert "visibility" not in payload


def test_remember_request_forwards_explicit_visibility() -> None:
    req = RememberRequest(content="shared fact", workspace="alpha", visibility="workspace")
    payload = req.model_dump(mode="json", exclude_none=True)
    assert payload["visibility"] == "workspace"
    assert payload["workspace"] == "alpha"


def test_classify_legacy_confidence_is_canonical_alias() -> None:
    response = ClassifyResponse.model_validate(
        {
            "classification_run_id": "11111111-1111-1111-1111-111111111111",
            "expires_at": "2026-07-14T13:00:00Z",
            "correlation_id": "11111111-1111-1111-1111-111111111111",
            "ingest_id": "22222222-2222-2222-2222-222222222222",
            "suggested_kind": "fact",
            "taxonomy_confidence": 0.8,
            "confidence": 0.1,
            "retention_confidence": 0.7,
            "retention_disposition": "retain",
            "reason": "test",
        }
    )
    assert response.confidence == response.taxonomy_confidence == 0.8


def test_recall_response_accepts_structured_evidence_blocks() -> None:
    """Issue #188: candidate-profile items carry the structured ``evidence``
    block and mirrored top-level ``epistemic_state``. The untyped item dicts
    must pass the model through unchanged — unknown/null states stay
    structured, never flattened into a numeric confidence field."""
    item = {
        "id": "0197c0de-0000-7000-8000-000000000001",
        "kind": "fact",
        "content": "served candidate item",
        "epistemic_state": "unknown",
        "warning_codes": ["unreviewed", "evidence_unknown", "risk_unknown"],
        "evidence": {
            "source": "v2_fresh_evaluation",
            "profile_key": "risk_aware_shadow_v1",
            "policy_version": "risk-aware-shadow-v1",
            "policy_artifact_digest": "sha256:" + "a" * 64,
            "decision_hash": "sha256:" + "b" * 64,
            "v2_resolution_status": "current",
            "epistemic_state": "unknown",
            "risk_state": "unknown",
            "retention_state": "unknown",
            "effective_assessment_refs": [],
        },
    }
    response = RecallResponse(
        working_set="[fact] served candidate item",
        item_count=1,
        byte_count=25,
        omitted_count=0,
        items=[item],
        recall_profile="exploratory",
        signals_version="recall-signals-v1",
        omitted_by_admission={},
    )
    served = response.items[0]
    assert served["evidence"]["epistemic_state"] == served["epistemic_state"]
    assert served["evidence"]["source"] == "v2_fresh_evaluation"
    # No numeric confidence was invented anywhere in the item.
    assert "trust_score" not in served
    assert "confidence" not in served

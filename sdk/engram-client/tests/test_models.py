"""Client-side model validation tests for the sensitivity enum.

The SDK's ``RememberRequest`` mirrors the server's Pydantic model so callers
get a validation error locally instead of a round trip to the API. The
product vocabulary is ``normal|sensitive|restricted`` — ``confidential`` is
not, and never was, a value the database accepts.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engram_client.models import ClassifyResponse, RememberRequest


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

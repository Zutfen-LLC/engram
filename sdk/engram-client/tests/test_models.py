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


def test_classify_legacy_confidence_is_canonical_alias() -> None:
    response = ClassifyResponse.model_validate(
        {
            "classification_run_id": "11111111-1111-1111-1111-111111111111",
            "expires_at": "2026-07-14T13:00:00Z",
            "suggested_kind": "fact",
            "taxonomy_confidence": 0.8,
            "confidence": 0.1,
            "retention_confidence": 0.7,
            "retention_disposition": "retain",
            "reason": "test",
        }
    )
    assert response.confidence == response.taxonomy_confidence == 0.8

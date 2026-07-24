"""OpenAPI determinism tests."""

from __future__ import annotations

import json

from engram_control_plane.api.app import create_app
from engram_control_plane.api.openapi import canonical_openapi_json


def test_openapi_export_is_deterministic() -> None:
    """Two builds of the OpenAPI document are byte-identical."""
    text1 = canonical_openapi_json(create_app())
    text2 = canonical_openapi_json(create_app())
    assert text1 == text2


def test_openapi_keys_are_sorted() -> None:
    """The canonical text has sorted top-level keys for stable diffs."""
    text = canonical_openapi_json(create_app())
    schema = json.loads(text)
    top_keys = list(schema.keys())
    assert top_keys == sorted(top_keys)


def test_openapi_documents_all_routes() -> None:
    """All scaffold routes appear in the OpenAPI document."""
    schema = json.loads(canonical_openapi_json(create_app()))
    paths = schema["paths"]
    assert "/health" in paths
    assert "/ready" in paths
    assert "/control/v1/meta" in paths

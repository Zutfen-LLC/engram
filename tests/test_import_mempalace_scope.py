"""MemPalace KG imports carry the same selected scope as drawer memories."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scripts.import_mempalace import _import_kg_triple


@pytest.mark.parametrize(
    ("visibility", "workspace"),
    [("private", None), ("workspace", "engineering"), ("tenant", None), ("public", None)],
)
def test_kg_import_forwards_selected_memory_scope(
    visibility: str, workspace: str | None
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert _import_kg_triple(
            client,
            "http://engram.test",
            {"subject": "a", "predicate": "rel", "object": "b"},
            30.0,
            visibility=visibility,
            workspace=workspace,
        )

    assert captured["visibility"] == visibility
    if workspace is None:
        assert "workspace" not in captured
    else:
        assert captured["workspace"] == workspace

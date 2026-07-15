from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from engram_hooks.config import HooksConfig
from engram_hooks.hooks import LifecycleHooks


class RecordingClient:
    def __init__(self) -> None:
        self.remember_calls: list[dict[str, Any]] = []

    async def classify(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            taxonomy_confidence=0.9,
            retention_confidence=0.9,
            retention_disposition="retain",
            classification_run_id="run-id",
            suggested_kind="fact",
            suggested_wing=None,
            suggested_room=None,
        )

    async def remember(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.remember_calls.append({"content": content, **kwargs})
        return SimpleNamespace(status="created")


async def test_session_end_routes_dedicated_source_type(tmp_path) -> None:
    hooks = LifecycleHooks(
        HooksConfig(
            base_url="http://engram.test",
            volatile_path=str(tmp_path / "volatile.jsonl"),
        )
    )
    recorder = RecordingClient()
    hooks._client = recorder

    result = await hooks.session_end({"content": "Durable session summary"})

    assert result.promoted == 1
    assert len(recorder.remember_calls) == 1
    call = recorder.remember_calls[0]
    correlation_id = call.pop("correlation_id")
    assert correlation_id is not None
    assert call == {
        "content": "Durable session summary",
        "kind": "fact",
        "wing": None,
        "room": None,
        "workspace": None,
        "source_type": "session_end",
        "classification_run_id": "run-id",
    }

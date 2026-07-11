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
            confidence=0.9, suggested_kind="fact", suggested_wing=None, suggested_room=None
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
    assert recorder.remember_calls == [
        {
            "content": "Durable session summary",
            "kind": "fact",
            "wing": None,
            "room": None,
            "workspace": None,
            "source_type": "session_end",
        }
    ]

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from engram_hooks.config import HooksConfig
from engram_hooks.hooks import LifecycleHooks


class Client:
    def __init__(self, disposition: str, confidence: float) -> None:
        self.disposition = disposition
        self.confidence = confidence
        self.classify_calls: list[dict[str, Any]] = []
        self.remember_calls: list[dict[str, Any]] = []

    async def classify(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.classify_calls.append({"content": content, **kwargs})
        return SimpleNamespace(
            classification_run_id="receipt",
            suggested_kind="decision",
            suggested_wing="engineering",
            suggested_room="architecture",
            taxonomy_confidence=0.2,
            retention_confidence=self.confidence,
            retention_disposition=self.disposition,
        )

    async def remember(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.remember_calls.append({"content": content, **kwargs})
        return SimpleNamespace(id="item")


def _hooks(tmp_path: Any, client: Client, threshold: float = 0.65) -> LifecycleHooks:
    hooks = LifecycleHooks(
        HooksConfig(
            base_url="http://test",
            volatile_path=str(tmp_path / "volatile.jsonl"),
            store_confidence_threshold=threshold,
        )
    )
    hooks._client = client
    return hooks


async def test_retain_above_threshold_remembers_receipt_and_server_taxonomy(tmp_path: Any) -> None:
    client = Client("retain", 0.9)
    detail = await _hooks(tmp_path, client)._route_candidate(
        "We decided to use Postgres", source_type="sync_turn"
    )
    assert detail["route"] == "remembered"
    assert client.classify_calls[0]["source_type"] == "sync_turn"
    assert client.remember_calls[0]["classification_run_id"] == "receipt"
    assert client.remember_calls[0]["kind"] == "decision"


@pytest.mark.parametrize(
    ("disposition", "confidence", "route"),
    [
        ("retain", 0.64, "parked"),
        ("transient", 0.95, "parked"),
        ("uncertain", 0.95, "parked"),
        ("noise", 0.0, "rejected"),
    ],
)
async def test_retention_matrix(
    tmp_path: Any, disposition: str, confidence: float, route: str
) -> None:
    client = Client(disposition, confidence)
    hooks = _hooks(tmp_path, client)
    detail = await hooks._route_candidate("Durable candidate text", source_type="pre_compress")
    assert detail["route"] == route
    assert bool(client.remember_calls) is (route == "remembered")
    if disposition == "noise":
        assert hooks.volatile.all() == []


def test_store_env_precedence_and_deprecated_config_compat(monkeypatch: Any) -> None:
    monkeypatch.setenv("ENGRAM_HOOKS_PROMOTE_THRESHOLD", "0.5")
    monkeypatch.setenv("ENGRAM_HOOKS_STORE_THRESHOLD", "0.8")
    assert HooksConfig().store_confidence_threshold == pytest.approx(0.8)
    monkeypatch.delenv("ENGRAM_HOOKS_STORE_THRESHOLD")
    assert HooksConfig().store_confidence_threshold == pytest.approx(0.5)
    assert HooksConfig(promote_confidence_threshold=0.7).store_confidence_threshold == 0.7

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
            ingest_id="ingest",
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
    assert client.remember_calls[0]["ingest_id"] == "ingest"
    assert client.remember_calls[0]["kind"] == "decision"


async def test_no_default_workspace_forwards_none_workspace_and_no_visibility(
    tmp_path: Any,
) -> None:
    """ENG-SCOPE-001: with no configured default_workspace, the hook forwards
    workspace=None and never sets visibility explicitly — the SDK omits it
    from the request, and the server derives 'private' (no widening)."""
    client = Client("retain", 0.9)
    hooks = _hooks(tmp_path, client)
    assert hooks.config.default_workspace is None
    await hooks._route_candidate("We decided to use Postgres", source_type="sync_turn")
    assert client.remember_calls[0]["workspace"] is None
    assert "visibility" not in client.remember_calls[0]
    assert client.classify_calls[0]["workspace"] is None


async def test_default_workspace_forwards_workspace_and_no_visibility(tmp_path: Any) -> None:
    """ENG-SCOPE-001: a configured default_workspace is forwarded as-is (still
    no explicit visibility) — the server derives 'workspace' from having a
    workspace, giving workspace-shared storage without the hook (or a model)
    having to choose the safe default itself."""
    client = Client("retain", 0.9)
    hooks = LifecycleHooks(
        HooksConfig(
            base_url="http://test",
            volatile_path=str(tmp_path / "volatile.jsonl"),
            store_confidence_threshold=0.65,
            default_workspace="team-alpha",
        )
    )
    hooks._client = client
    await hooks._route_candidate("We decided to use Postgres", source_type="sync_turn")
    assert client.remember_calls[0]["workspace"] == "team-alpha"
    assert "visibility" not in client.remember_calls[0]
    assert client.classify_calls[0]["workspace"] == "team-alpha"


async def test_zero_threshold_remembers_zero_confidence_retain(tmp_path: Any) -> None:
    client = Client("retain", 0.0)
    hooks = _hooks(tmp_path, client, threshold=0.0)
    detail = await hooks._route_candidate("Durable candidate text", source_type="sync_turn")
    assert detail["route"] == "remembered"
    assert len(client.remember_calls) == 1


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


def test_explicit_zero_store_threshold_is_preserved(monkeypatch: Any) -> None:
    assert HooksConfig(store_confidence_threshold=0.0).store_confidence_threshold == 0.0
    monkeypatch.setenv("ENGRAM_HOOKS_PROMOTE_THRESHOLD", "0.5")
    monkeypatch.setenv("ENGRAM_HOOKS_STORE_THRESHOLD", "0")
    config = HooksConfig()
    assert config.store_confidence_threshold == 0.0
    assert config.promote_confidence_threshold == 0.0

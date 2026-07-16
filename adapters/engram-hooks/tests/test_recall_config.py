"""Read-side environment defaults and safety clamps."""
from __future__ import annotations

from engram_hooks.config import HooksConfig


def test_recall_config_defaults(monkeypatch) -> None:
    for name in (
        "ENGRAM_HOOKS_RECALL_ENABLED",
        "ENGRAM_HOOKS_RECALL_TIMEOUT",
        "ENGRAM_HOOKS_RECALL_ITEM_BUDGET",
        "ENGRAM_HOOKS_RECALL_BYTE_BUDGET",
        "ENGRAM_HOOKS_RECALL_MAX_CONTEXT_BYTES",
        "ENGRAM_HOOKS_RECALL_FOLLOWUP_TURNS",
        "ENGRAM_HOOKS_RECALL_BREAKER_FAILURES",
        "ENGRAM_HOOKS_RECALL_MAX_SESSIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    config = HooksConfig()
    assert config.recall_enabled is False
    assert config.recall_timeout == 1.5
    assert config.recall_item_budget == 5
    assert config.recall_byte_budget == 8192
    assert config.recall_max_context_bytes == 12000
    assert config.recall_followup_turns == 3
    assert config.recall_breaker_failures == 3
    assert config.recall_max_sessions == 512


def test_recall_config_malformed_values_fall_back(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_TIMEOUT", "broken")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_ITEM_BUDGET", "broken")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_BYTE_BUDGET", "broken")
    config = HooksConfig()
    assert config.recall_timeout == 1.5
    assert config.recall_item_budget == 5
    assert config.recall_byte_budget == 8192

    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_TIMEOUT", "nan")
    assert HooksConfig().recall_timeout == 1.5


def test_recall_config_values_are_clamped(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_ENABLED", "true")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_TIMEOUT", "99")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_ITEM_BUDGET", "0")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_BYTE_BUDGET", "0")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_MAX_CONTEXT_BYTES", "99999999")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_FOLLOWUP_TURNS", "99")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_BREAKER_FAILURES", "0")
    monkeypatch.setenv("ENGRAM_HOOKS_RECALL_MAX_SESSIONS", "0")
    config = HooksConfig()
    assert config.recall_enabled is True
    assert config.recall_timeout == 10.0
    assert config.recall_item_budget == 1
    assert config.recall_byte_budget == 256
    assert config.recall_max_context_bytes == 1_000_000
    assert config.recall_followup_turns == 10
    assert config.recall_breaker_failures == 1
    assert config.recall_max_sessions == 1

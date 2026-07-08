"""Tests for ``detect_prepare_memory_write`` — the native-hook probe.

Covers the three detection outcomes: Hermes absent, Hermes present with the
hook, Hermes present without it. This is the branch point that decides
whether ``install()`` patches anything at all.
"""

from __future__ import annotations

from typing import Any

from engram_hooks.hooks import detect_prepare_memory_write


def test_detect_reports_hermes_absent(no_hermes: None) -> None:
    result = detect_prepare_memory_write()
    assert result == {
        "hermes_present": False,
        "hook_present": False,
        "provider": None,
        "error": result["error"],
    }
    assert result["error"]  # non-empty import error message


def test_detect_reports_native_hook_present(fake_hermes_native: dict[str, Any]) -> None:
    result = detect_prepare_memory_write()
    assert result["hermes_present"] is True
    assert result["hook_present"] is True
    assert result["provider"] == "hermes_agent.memory.provider.MemoryProvider"
    assert result["error"] is None


def test_detect_reports_hook_missing(fake_hermes_shim_needed: dict[str, Any]) -> None:
    result = detect_prepare_memory_write()
    assert result["hermes_present"] is True
    assert result["hook_present"] is False
    assert result["provider"] == "hermes_agent.memory.provider.MemoryProvider"
    assert result["error"] is None

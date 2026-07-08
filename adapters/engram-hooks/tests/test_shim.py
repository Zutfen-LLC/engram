"""Tests for the compatibility shim: detection routing, patch application,
guard allow/reject short-circuiting, idempotency, and patch-failure behavior.

These are the compat-path acceptance criteria for ENG-HERMES-001:

* native hook present -> no monkey-patch
* native hook absent -> dispatch sites patched, logged, inspectable
* patched dispatch actively rejects -> original Hermes writer never runs
* patched dispatch allows -> original Hermes writer runs
* install() is idempotent -> a second call does not double-wrap
* missing dispatch sites (API drift) -> loud, diagnostic failure
"""

from __future__ import annotations

from typing import Any

import pytest

from engram_hooks import HooksConfig
from engram_hooks.hooks import (
    _SHIM_MARKER,
    LifecycleHooks,
    get_active_hooks,
    install,
    install_compat_shim,
)


def _config(**overrides: Any) -> HooksConfig:
    # No ENGRAM_BASE_URL: LifecycleHooks degrades to volatile-only, no network.
    return HooksConfig(base_url="", **overrides)


# ---------------------------------------------------------------------------
# Native hook present -> no patch
# ---------------------------------------------------------------------------


def test_native_hook_present_skips_patch(fake_hermes_native: dict[str, Any], tmp_path) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))
    status = install_compat_shim(hooks)

    assert status.native_hook_available is True
    assert status.compat_shim_installed is False
    assert status.patched_modules == []
    assert status.automatic_capture_active is True

    # The dispatch sites must be untouched — no shim marker anywhere.
    tool_executor = fake_hermes_native["tool_executor"]
    assert not getattr(tool_executor.memory, _SHIM_MARKER, False)


# ---------------------------------------------------------------------------
# Native hook absent -> patch applied
# ---------------------------------------------------------------------------


def test_hook_missing_patches_both_dispatch_sites(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))
    status = install_compat_shim(hooks)

    assert status.native_hook_available is False
    assert status.compat_shim_installed is True
    assert status.automatic_capture_active is True
    assert set(status.patched_modules) == {
        "hermes_agent.tools.tool_executor",
        "hermes_agent.runtime.agent_runtime_helpers",
    }

    tool_executor = fake_hermes_shim_needed["tool_executor"]
    agent_runtime_helpers = fake_hermes_shim_needed["agent_runtime_helpers"]
    assert getattr(tool_executor.memory, _SHIM_MARKER, False) is True
    assert getattr(agent_runtime_helpers.memory, _SHIM_MARKER, False) is True


def test_guard_reject_short_circuits_original_writer(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))
    install_compat_shim(hooks)

    # install_compat_shim() alone doesn't set ACTIVE_HOOKS — only install() does.
    # The wrapper falls back to the stateless guard when no hooks are active,
    # which still actively rejects ephemeral content.
    tool_executor = fake_hermes_shim_needed["tool_executor"]
    original = fake_hermes_shim_needed["memory_fns"]["tool_executor"]

    result = tool_executor.memory("currently editing line 5")

    assert result["handled"] is True
    assert result["action"] == "reject"
    assert original.calls == []  # the underlying Hermes writer was never reached


def test_guard_allow_reaches_original_writer(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))
    install_compat_shim(hooks)

    tool_executor = fake_hermes_shim_needed["tool_executor"]
    original = fake_hermes_shim_needed["memory_fns"]["tool_executor"]

    result = tool_executor.memory("Always use lowercase table names in this schema.")

    assert result == "wrote:Always use lowercase table names in this schema."
    assert len(original.calls) == 1
    assert original.calls[0][0][0] == "Always use lowercase table names in this schema."


def test_guard_uses_active_hooks_when_installed_via_install(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    """When install() (not just install_compat_shim) has run, the wrapper
    dispatches through the live LifecycleHooks rather than the stateless
    guard — same guard logic, but proves the dynamic lookup wiring works.
    """
    install(_config(volatile_path=str(tmp_path / "v.jsonl")))
    assert get_active_hooks() is not None

    tool_executor = fake_hermes_shim_needed["tool_executor"]
    original = fake_hermes_shim_needed["memory_fns"]["tool_executor"]

    reject_result = tool_executor.memory("cursor is at line 3")
    assert reject_result["action"] == "reject"
    assert original.calls == []

    allow_result = tool_executor.memory("The staging database is named engram_staging.")
    assert allow_result == "wrote:The staging database is named engram_staging."
    assert len(original.calls) == 1


# ---------------------------------------------------------------------------
# Idempotency: a second install does not double-wrap
# ---------------------------------------------------------------------------


def test_install_compat_shim_is_idempotent(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))

    first = install_compat_shim(hooks)
    tool_executor = fake_hermes_shim_needed["tool_executor"]
    wrapped_once = tool_executor.memory

    second = install_compat_shim(hooks)
    wrapped_twice = tool_executor.memory

    assert first.compat_shim_installed is True
    assert second.compat_shim_installed is True
    # The exact same function object — install_compat_shim recognized the
    # existing marker and did not wrap the wrapper.
    assert wrapped_once is wrapped_twice

    # A single guard evaluation per call, not two nested ones: calling the
    # (still) wrapped function with an allowed candidate reaches the original
    # exactly once.
    original = fake_hermes_shim_needed["memory_fns"]["tool_executor"]
    tool_executor.memory("Always deploy from the release branch, never main.")
    assert len(original.calls) == 1


def test_install_top_level_is_idempotent_across_repeated_calls(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    cfg = _config(volatile_path=str(tmp_path / "v.jsonl"))
    result1 = install(cfg)
    result2 = install(cfg)

    assert result1["status"].compat_shim_installed is True
    assert result2["status"].compat_shim_installed is True

    tool_executor = fake_hermes_shim_needed["tool_executor"]
    original = fake_hermes_shim_needed["memory_fns"]["tool_executor"]
    tool_executor.memory("The primary region for this deployment is us-east-1.")
    assert len(original.calls) == 1  # not called twice by a doubled wrapper


# ---------------------------------------------------------------------------
# Hermes absent -> shim inactive, not an error
# ---------------------------------------------------------------------------


def test_no_hermes_shim_inactive_not_fatal(no_hermes: None, tmp_path) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))
    status = install_compat_shim(hooks)

    assert status.native_hook_available is False
    assert status.compat_shim_installed is False
    assert status.automatic_capture_active is False
    assert status.failure_reason is not None


# ---------------------------------------------------------------------------
# Patch failure: Hermes present, hook absent, dispatch sites missing (drift)
# ---------------------------------------------------------------------------


def test_patch_failure_when_dispatch_sites_missing(
    fake_hermes_no_dispatch_sites: dict[str, Any], tmp_path
) -> None:
    hooks = LifecycleHooks(_config(volatile_path=str(tmp_path / "v.jsonl")))
    status = install_compat_shim(hooks)

    assert status.native_hook_available is False
    assert status.compat_shim_installed is False
    assert status.automatic_capture_active is False
    assert status.failure_reason is not None
    assert "hermes_agent.tools.tool_executor" in status.failure_reason
    assert "hermes_agent.runtime.agent_runtime_helpers" in status.failure_reason


# ---------------------------------------------------------------------------
# require_automatic_capture: fail loudly instead of silently degrading
# ---------------------------------------------------------------------------


def test_install_raises_when_automatic_capture_required_but_unavailable(
    no_hermes: None, tmp_path
) -> None:
    from engram_hooks import AutomaticCaptureUnavailable

    cfg = _config(
        volatile_path=str(tmp_path / "v.jsonl"),
        require_automatic_capture=True,
    )
    with pytest.raises(AutomaticCaptureUnavailable) as excinfo:
        install(cfg)

    assert excinfo.value.status.automatic_capture_active is False
    assert "require_automatic_capture" in str(excinfo.value)


def test_install_does_not_raise_when_capture_not_required(no_hermes: None, tmp_path) -> None:
    cfg = _config(volatile_path=str(tmp_path / "v.jsonl"), require_automatic_capture=False)
    result = install(cfg)  # must not raise
    assert result["status"].automatic_capture_active is False


def test_install_satisfies_requirement_via_native_hook(
    fake_hermes_native: dict[str, Any], tmp_path
) -> None:
    cfg = _config(volatile_path=str(tmp_path / "v.jsonl"), require_automatic_capture=True)
    result = install(cfg)  # must not raise
    assert result["status"].native_hook_available is True
    assert result["status"].automatic_capture_active is True


def test_install_satisfies_requirement_via_compat_shim(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    cfg = _config(volatile_path=str(tmp_path / "v.jsonl"), require_automatic_capture=True)
    result = install(cfg)  # must not raise
    assert result["status"].compat_shim_installed is True
    assert result["status"].automatic_capture_active is True


def test_compat_shim_disabled_reports_inactive_without_raising(
    fake_hermes_shim_needed: dict[str, Any], tmp_path
) -> None:
    cfg = _config(volatile_path=str(tmp_path / "v.jsonl"), enable_compat_shim=False)
    result = install(cfg)
    assert result["status"].automatic_capture_active is False
    assert "ENGRAM_HOOKS_COMPAT_SHIM" in result["status"].failure_reason

    tool_executor = fake_hermes_shim_needed["tool_executor"]
    assert not getattr(tool_executor.memory, _SHIM_MARKER, False)

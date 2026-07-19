"""Shared fixtures for the engram-hooks test suite.

None of these tests require a real Hermes checkout or a live Engram server:

* Guard / detection / shim tests build a **fake Hermes** package tree in
  ``sys.modules`` that mirrors the module layouts ``engram_hooks.hooks``
  targets (historical ``hermes_agent.*`` and current local-install
  ``agent.*`` paths). This lets us exercise "hook present" vs. "hook absent,
  patch these dispatch sites" vs. "Hermes present but API drifted"
  deterministically in CI.
* Lifecycle-hook tests construct :class:`~engram_hooks.HooksConfig` with no
  ``ENGRAM_BASE_URL``, so ``LifecycleHooks`` degrades to volatile-only mode —
  no network, no mocked SDK client needed for guard/routing behavior.

Every test that touches module-level state (``ACTIVE_HOOKS``, ``ACTIVE_STATUS``,
or the fake ``sys.modules`` entries) resets it via the ``clean_hooks_state``
autouse fixture so tests never leak state into each other.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import engram_hooks.hooks as hooks_module

_FAKE_MODULE_NAMES = (
    "hermes_agent",
    "hermes_agent.memory",
    "hermes_agent.memory.provider",
    "hermes_agent.tools",
    "hermes_agent.tools.tool_executor",
    "hermes_agent.runtime",
    "hermes_agent.runtime.agent_runtime_helpers",
    "agent",
    "agent.memory_provider",
    "agent.tool_executor",
    "agent.agent_runtime_helpers",
    "tools",
    "tools.memory_tool",
    "plugins",
    "plugins.memory",
    "hermes_cli",
    "hermes_cli.config",
    "hermes_cli.memory_setup",
    "hermes_constants",
    "_hermes_user_memory",
    "_hermes_user_memory.engram_memory",
    "_hermes_user_memory.engram_memory.recall_bridge",
    "engram_memory",
    "engram_memory.recall_bridge",
)


@pytest.fixture(autouse=True)
def clean_hooks_state():
    """Reset engram_hooks' module-level install state around every test.

    Without this, ``install()`` in one test would leak ``ACTIVE_HOOKS`` /
    ``ACTIVE_STATUS`` into the next, and any fake ``hermes_agent.*`` modules
    left in ``sys.modules`` would bleed between tests that expect Hermes to be
    absent vs. present.
    """
    for name in _FAKE_MODULE_NAMES:
        sys.modules.pop(name, None)
    hooks_module.ACTIVE_HOOKS = None
    hooks_module.ACTIVE_STATUS = None
    hooks_module.ACTIVE_WRITE_INTERCEPTOR = None
    yield
    for name in _FAKE_MODULE_NAMES:
        sys.modules.pop(name, None)
    hooks_module.ACTIVE_HOOKS = None
    hooks_module.ACTIVE_STATUS = None
    hooks_module.ACTIVE_WRITE_INTERCEPTOR = None


class RecordingMemory:
    """A callable that records every invocation, standing in for Hermes' real
    ``memory()`` dispatch function so tests can assert whether it was reached.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, content: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append(((content, *args), kwargs))
        return f"wrote:{content}"


def _install_fake_hermes(
    *,
    hook_present: bool,
    with_dispatch_sites: bool = True,
    layout: str = "hermes_agent",
) -> dict[str, Any]:
    """Build and register the fake Hermes package tree.

    Returns a dict with the constructed pieces (``provider_cls``,
    ``tool_executor``, ``agent_runtime_helpers``, ``memory_fns``) so tests can
    assert against them directly.
    """
    if layout == "hermes_agent":
        root = types.ModuleType("hermes_agent")
        memory_pkg = types.ModuleType("hermes_agent.memory")
        provider_mod = types.ModuleType("hermes_agent.memory.provider")
        tools_pkg = types.ModuleType("hermes_agent.tools")
        tool_executor_mod = types.ModuleType("hermes_agent.tools.tool_executor")
        runtime_pkg = types.ModuleType("hermes_agent.runtime")
        agent_runtime_helpers_mod = types.ModuleType("hermes_agent.runtime.agent_runtime_helpers")
        provider_module_name = "hermes_agent.memory.provider"
    elif layout == "agent":
        root = types.ModuleType("agent")
        memory_pkg = None
        provider_mod = types.ModuleType("agent.memory_provider")
        tools_pkg = None
        tool_executor_mod = types.ModuleType("agent.tool_executor")
        runtime_pkg = None
        agent_runtime_helpers_mod = types.ModuleType("agent.agent_runtime_helpers")
        provider_module_name = "agent.memory_provider"
    else:
        raise ValueError(f"unknown fake Hermes layout: {layout}")

    if hook_present:

        class MemoryProvider:
            def prepare_memory_write(self, content: str, **kwargs: Any) -> Any:
                raise NotImplementedError

    else:

        class MemoryProvider:
            pass

    # __module__ defaults to this conftest module (where the class statement
    # actually executes); force it to the fake module's dotted name so
    # detect_prepare_memory_write()'s `f"{cls.__module__}.{cls.__qualname__}"`
    # matches what it would report for a real Hermes install.
    MemoryProvider.__module__ = provider_module_name
    MemoryProvider.__qualname__ = "MemoryProvider"
    provider_mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]

    memory_fns: dict[str, RecordingMemory] = {}
    if with_dispatch_sites:
        memory_fns["tool_executor"] = RecordingMemory()
        tool_executor_mod.memory = memory_fns["tool_executor"]  # type: ignore[attr-defined]
        memory_fns["agent_runtime_helpers"] = RecordingMemory()
        agent_runtime_helpers_mod.memory = memory_fns["agent_runtime_helpers"]  # type: ignore[attr-defined]

    for mod in (
        root,
        memory_pkg,
        provider_mod,
        tools_pkg,
        tool_executor_mod,
        runtime_pkg,
        agent_runtime_helpers_mod,
    ):
        if mod is not None:
            sys.modules[mod.__name__] = mod

    return {
        "provider_cls": MemoryProvider,
        "tool_executor": tool_executor_mod,
        "agent_runtime_helpers": agent_runtime_helpers_mod,
        "memory_fns": memory_fns,
    }


@pytest.fixture
def fake_hermes_native() -> dict[str, Any]:
    """Fake Hermes with ``prepare_memory_write`` present on ``MemoryProvider``."""
    return _install_fake_hermes(hook_present=True)


@pytest.fixture
def fake_hermes_shim_needed() -> dict[str, Any]:
    """Fake Hermes with the hook absent and both dispatch sites patchable."""
    return _install_fake_hermes(hook_present=False)


@pytest.fixture
def fake_hermes_no_dispatch_sites() -> dict[str, Any]:
    """Fake Hermes with the hook absent and *no* known dispatch attribute —
    models upstream API drift where our patch targets no longer exist.
    """
    return _install_fake_hermes(hook_present=False, with_dispatch_sites=False)


@pytest.fixture
def fake_current_hermes_native() -> dict[str, Any]:
    """Fake current local Hermes layout with native prepare_memory_write present."""
    return _install_fake_hermes(hook_present=True, layout="agent")


@pytest.fixture
def fake_current_hermes_shim_needed() -> dict[str, Any]:
    """Fake current local Hermes layout with hook absent and patchable dispatch sites."""
    return _install_fake_hermes(hook_present=False, layout="agent")


@pytest.fixture
def no_hermes() -> None:
    """Explicit fixture (no-op) documenting "Hermes is not installed at all".

    ``clean_hooks_state`` already pops any fake hermes_agent modules before
    each test, so this just names the precondition for readability.
    """
    return None

"""Pinned stock-Hermes governed-write compatibility tests."""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from engram_hooks import AutomaticCaptureUnavailable, HooksConfig
from engram_hooks.guards import is_allowed, prepare_memory_write_guard
from engram_hooks.hooks import (
    _SHIM_MARKER,
    HERMES_REFERENCE_REPOSITORY,
    HERMES_REFERENCE_SHA,
    install,
)

_CONTRACT_ROOT = Path(__file__).parent / "fixtures" / "hermes_stock_36f2a966"


def _config(tmp_path: Path, **overrides: Any) -> HooksConfig:
    return HooksConfig(
        base_url="",
        volatile_path=str(tmp_path / "volatile.jsonl"),
        **overrides,
    )


class _Manager:
    def __init__(self) -> None:
        self.notifications: list[tuple[Any, dict[str, Any]]] = []

    def notify_memory_tool_write(
        self,
        result: Any,
        args: dict[str, Any],
        *,
        build_metadata: Any = None,
    ) -> None:
        del build_metadata
        self.notifications.append((result, args))


class _Agent:
    def __init__(self, store: Any) -> None:
        self._memory_store = store
        self._memory_manager = _Manager()


class _GovernedWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.submissions: list[str] = []

    def __call__(
        self,
        *,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None,
        old_text: str | None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "action": action,
                "target": target,
                "content": content,
                "metadata": metadata,
                "old_text": old_text,
            }
        )
        verdict = prepare_memory_write_guard(content)
        if not is_allowed(verdict):
            return {
                "handled": True,
                "result": {
                    "success": False,
                    "error": f"Rejected by Engram guard: {verdict.get('reason')}",
                },
            }
        self.submissions.append(content)
        return {
            "handled": True,
            "result": {"success": True, "message": "Submitted to Engram"},
        }


@pytest.fixture
def stock_contract(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.syspath_prepend(str(_CONTRACT_ROOT))
    for name in (
        "agent",
        "agent.memory_provider",
        "agent.tool_executor",
        "agent.agent_runtime_helpers",
        "tools",
        "tools.memory_tool",
    ):
        sys.modules.pop(name, None)
    memory_tool = importlib.import_module("tools.memory_tool")
    return {
        "memory_tool": memory_tool,
        "tool_executor": importlib.import_module("agent.tool_executor"),
        "runtime_helpers": importlib.import_module("agent.agent_runtime_helpers"),
        "provider": importlib.import_module("agent.memory_provider"),
    }


@pytest.mark.parametrize("executor_name", ["tool_executor", "runtime_helpers"])
def test_accepted_add_routes_once_without_native_mutation(
    stock_contract: dict[str, Any], tmp_path: Path, executor_name: str
) -> None:
    writer = _GovernedWriter()
    status = install(_config(tmp_path), write_interceptor=writer)["status"]
    store = stock_contract["memory_tool"].MemoryStore()
    agent = _Agent(store)

    result = json.loads(
        stock_contract[executor_name].execute_memory(
            agent,
            {
                "action": "add",
                "target": "memory",
                "content": "Always use PostgreSQL 16 for the Engram database.",
            },
        )
    )

    assert status.activation_mode == "stock_compat"
    assert status.patched_modules == ["tools.memory_tool"]
    assert result == {
        "success": True,
        "message": "Submitted to Engram",
        "provider": "engram",
        "native_write": False,
    }
    assert writer.submissions == ["Always use PostgreSQL 16 for the Engram database."]
    assert store.entries == {"memory": [], "user": []}


@pytest.mark.parametrize("executor_name", ["tool_executor", "runtime_helpers"])
def test_rejected_add_never_submits_or_mutates_native_store(
    stock_contract: dict[str, Any], tmp_path: Path, executor_name: str
) -> None:
    writer = _GovernedWriter()
    install(_config(tmp_path), write_interceptor=writer)
    store = stock_contract["memory_tool"].MemoryStore()

    result = json.loads(
        stock_contract[executor_name].execute_memory(
            _Agent(store),
            {"action": "add", "target": "user", "content": "currently editing line 5"},
        )
    )

    assert result["success"] is False
    assert "Rejected by Engram guard" in result["error"]
    assert result["native_write"] is False
    assert writer.submissions == []
    assert store.entries == {"memory": [], "user": []}


def test_contract_has_nested_lazy_import_shape_not_module_memory_attributes(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    writer = _GovernedWriter()
    install(_config(tmp_path), write_interceptor=writer)

    assert not hasattr(stock_contract["tool_executor"], "memory")
    assert not hasattr(stock_contract["runtime_helpers"], "memory")
    assert getattr(stock_contract["memory_tool"].memory_tool, _SHIM_MARKER) is True
    assert HERMES_REFERENCE_REPOSITORY == "NousResearch/hermes-agent"
    assert HERMES_REFERENCE_SHA == "36f2a966c7f9f69987494b867c3dcf96b69a5766"


def test_native_prepare_status_restores_and_skips_compat_wrapper(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    provider_class = stock_contract["provider"].MemoryProvider
    provider_class.prepare_memory_write = lambda self, **kwargs: None
    original = stock_contract["memory_tool"].memory_tool

    status = install(_config(tmp_path), write_interceptor=_GovernedWriter())["status"]

    assert status.activation_mode == "native_prepare"
    assert status.native_hook_available is True
    assert status.compat_shim_installed is False
    assert stock_contract["memory_tool"].memory_tool is original


def test_reinstall_updates_provider_without_double_wrap_or_duplicate_submission(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    first_writer = _GovernedWriter()
    second_writer = _GovernedWriter()
    install(_config(tmp_path), write_interceptor=first_writer)
    wrapped_once = stock_contract["memory_tool"].memory_tool
    install(_config(tmp_path), write_interceptor=second_writer)
    wrapped_twice = stock_contract["memory_tool"].memory_tool
    store = stock_contract["memory_tool"].MemoryStore()

    stock_contract["tool_executor"].execute_memory(
        _Agent(store),
        {
            "action": "add",
            "target": "memory",
            "content": "The active production region is us-east-1.",
        },
    )

    assert wrapped_once is wrapped_twice
    assert first_writer.calls == []
    assert len(second_writer.calls) == 1
    assert second_writer.submissions == ["The active production region is us-east-1."]
    assert store.entries["memory"] == []


def test_full_hooks_module_replacement_updates_surviving_wrapper_provider(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    """A Hermes plugin reload can retain tools.memory_tool but replace hooks.py."""
    import engram_hooks
    import engram_hooks.hooks as old_hooks_module

    first_writer = _GovernedWriter()
    old_hooks_module.install(_config(tmp_path), write_interceptor=first_writer)
    surviving_wrapper = stock_contract["memory_tool"].memory_tool

    sys.modules.pop("engram_hooks.hooks", None)
    if hasattr(engram_hooks, "hooks"):
        delattr(engram_hooks, "hooks")
    new_hooks_module = importlib.import_module("engram_hooks.hooks")
    second_writer = _GovernedWriter()
    new_hooks_module.install(_config(tmp_path), write_interceptor=second_writer)
    store = stock_contract["memory_tool"].MemoryStore()

    stock_contract["runtime_helpers"].execute_memory(
        _Agent(store),
        {
            "action": "add",
            "target": "memory",
            "content": "The durable reload policy uses the newest provider instance.",
        },
    )

    assert stock_contract["memory_tool"].memory_tool is surviving_wrapper
    assert first_writer.calls == []
    assert len(second_writer.calls) == 1
    assert store.entries["memory"] == []
    sys.modules["engram_hooks.hooks"] = old_hooks_module
    engram_hooks.hooks = old_hooks_module


def test_install_upgrades_legacy_global_callback_wrapper(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    memory_tool_module = stock_contract["memory_tool"]
    native_writer = memory_tool_module.memory_tool

    def legacy_wrapper(**kwargs: Any) -> str:
        return native_writer(**kwargs)

    setattr(legacy_wrapper, _SHIM_MARKER, True)
    legacy_wrapper.__engram_hooks_original__ = native_writer  # type: ignore[attr-defined]
    memory_tool_module.memory_tool = legacy_wrapper
    writer = _GovernedWriter()

    status = install(_config(tmp_path), write_interceptor=writer)["status"]
    store = memory_tool_module.MemoryStore()
    result = json.loads(
        stock_contract["tool_executor"].execute_memory(
            _Agent(store),
            {
                "action": "add",
                "target": "memory",
                "content": "An upgraded plugin uses its newly registered Engram provider.",
            },
        )
    )

    assert status.activation_mode == "stock_compat"
    assert memory_tool_module.memory_tool is not legacy_wrapper
    assert len(writer.calls) == 1
    assert result["provider"] == "engram"
    assert store.entries["memory"] == []


def test_disabling_compatibility_restores_native_behavior(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    writer = _GovernedWriter()
    install(_config(tmp_path), write_interceptor=writer)
    status = install(
        _config(tmp_path, enable_compat_shim=False), write_interceptor=writer
    )["status"]
    store = stock_contract["memory_tool"].MemoryStore()

    result = json.loads(
        stock_contract["tool_executor"].execute_memory(
            _Agent(store),
            {
                "action": "add",
                "target": "memory",
                "content": "This deliberate recall-only call uses native storage.",
            },
        )
    )

    assert status.activation_mode == "recall_only"
    assert status.automatic_capture_active is False
    assert result["success"] is True
    assert store.entries["memory"] == [
        "This deliberate recall-only call uses native storage."
    ]
    assert writer.calls == []


def test_reinstall_without_provider_restores_native_boundary(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    writer = _GovernedWriter()
    install(_config(tmp_path), write_interceptor=writer)

    status = install(_config(tmp_path))["status"]
    store = stock_contract["memory_tool"].MemoryStore()
    result = json.loads(
        stock_contract["tool_executor"].execute_memory(
            _Agent(store),
            {
                "action": "add",
                "target": "memory",
                "content": "Recall-only mode deliberately restores stock persistence.",
            },
        )
    )

    assert status.activation_mode == "recall_only"
    assert status.automatic_capture_active is False
    assert result["success"] is True
    assert store.entries["memory"] == [
        "Recall-only mode deliberately restores stock persistence."
    ]
    assert writer.calls == []


def test_add_containing_batch_is_rejected_atomically(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    writer = _GovernedWriter()
    install(_config(tmp_path), write_interceptor=writer)
    store = stock_contract["memory_tool"].MemoryStore()

    result = json.loads(
        stock_contract["runtime_helpers"].execute_memory(
            _Agent(store),
            {
                "target": "memory",
                "operations": [
                    {"action": "remove", "old_text": "stale"},
                    {"action": "add", "content": "Always deploy from release branches."},
                ],
            },
        )
    )

    assert result["success"] is False
    assert "add-containing memory batches" in result["error"]
    assert writer.calls == []
    assert store.entries == {"memory": [], "user": []}


def test_required_capture_fails_loudly_on_api_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = types.ModuleType("agent")
    provider_mod = types.ModuleType("agent.memory_provider")
    provider_mod.MemoryProvider = type("MemoryProvider", (), {})
    tools = types.ModuleType("tools")
    memory_tool = types.ModuleType("tools.memory_tool")
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", provider_mod)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.memory_tool", memory_tool)

    with pytest.raises(AutomaticCaptureUnavailable) as excinfo:
        install(
            _config(tmp_path, require_automatic_capture=True),
            write_interceptor=_GovernedWriter(),
        )

    message = str(excinfo.value)
    assert "tools.memory_tool.memory_tool" in message
    assert HERMES_REFERENCE_SHA in message
    assert excinfo.value.status.activation_mode == "incompatible"


def test_required_capture_fails_without_registered_provider(
    stock_contract: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(AutomaticCaptureUnavailable) as excinfo:
        install(_config(tmp_path, require_automatic_capture=True))

    assert "no Engram provider write interceptor" in str(excinfo.value)
    assert excinfo.value.status.automatic_capture_active is False

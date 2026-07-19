"""Pinned stock-Hermes provider discovery, status, and activation regressions."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from engram_hooks import AutomaticCaptureUnavailable
from engram_hooks.hooks import _SHIM_MARKER

_CONTRACT_ROOT = Path(__file__).parent / "fixtures" / "hermes_stock_36f2a966"
_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_plugin" / "engram_memory"


class _Manager:
    def notify_memory_tool_write(
        self,
        result: Any,
        args: dict[str, Any],
        *,
        build_metadata: Any = None,
    ) -> None:
        del result, args, build_metadata


class _Agent:
    def __init__(self, store: Any) -> None:
        self._memory_store = store
        self._memory_manager = _Manager()


@pytest.fixture
def pinned_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    hermes_home = tmp_path / "hermes-home"
    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "engram_memory").symlink_to(_PLUGIN_DIR, target_is_directory=True)

    monkeypatch.syspath_prepend(str(_CONTRACT_ROOT))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ENGRAM_BASE_URL", "https://engram.example.com")
    monkeypatch.setenv("ENGRAM_API_KEY", "eng_test_discovery_key")
    monkeypatch.setenv("ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE", "true")
    monkeypatch.setenv("ENGRAM_HOOKS_VOLATILE_PATH", str(tmp_path / "volatile.jsonl"))

    memory_plugins = importlib.import_module("plugins.memory")
    memory_status = importlib.import_module("hermes_cli.memory_setup")
    config = importlib.import_module("hermes_cli.config")
    config.CONFIG = {"memory": {"provider": "engram_memory"}}
    memory_tool = importlib.import_module("tools.memory_tool")
    executor = importlib.import_module("agent.tool_executor")
    return {
        "plugins": memory_plugins,
        "status": memory_status,
        "memory_tool": memory_tool,
        "executor": executor,
    }


def test_discovery_and_status_construct_without_activation(
    pinned_runtime: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    memory_plugins = pinned_runtime["plugins"]
    memory_tool = pinned_runtime["memory_tool"]
    native_memory_tool = memory_tool.memory_tool

    with (
        patch("engram_hooks.install", side_effect=AssertionError("discovery activated"))
        as install_mock,
        patch("engram_hooks.hooks._restore_memory_tool") as restore_mock,
    ):
        discovered = memory_plugins.discover_memory_providers()
        provider = memory_plugins.load_memory_provider("engram_memory")
        pinned_runtime["status"].cmd_status(None)

    assert any(name == "engram_memory" and available for name, _, available in discovered)
    assert provider is not None
    assert provider.name == "engram_memory"
    assert provider.is_available() is True
    assert provider._activation_mode == "uninitialized"
    assert provider._initialized is False
    install_mock.assert_not_called()
    restore_mock.assert_not_called()
    assert memory_tool.memory_tool is native_memory_tool
    assert not getattr(memory_tool.memory_tool, _SHIM_MARKER, False)

    output = capsys.readouterr().out
    assert "Provider:  engram_memory" in output
    assert "Plugin:    installed ✓" in output
    assert "Status:    available ✓" in output
    assert "Plugin:    NOT installed ✗" not in output


@pytest.mark.asyncio
async def test_real_initialize_intercepts_once_and_refreshes_callback_ownership(
    pinned_runtime: dict[str, Any],
) -> None:
    import engram_hooks

    memory_plugins = pinned_runtime["plugins"]
    memory_tool = pinned_runtime["memory_tool"]
    executor = pinned_runtime["executor"]
    first_provider = memory_plugins.load_memory_provider("engram_memory")
    assert first_provider is not None
    first_submissions: list[str] = []

    async def first_remember(content: str, metadata: dict[str, Any] | None = None) -> None:
        del metadata
        first_submissions.append(content)

    first_provider._async_remember = first_remember
    with patch("engram_hooks.install", wraps=engram_hooks.install) as install_spy:
        first_provider.initialize("session-one", agent_context="primary")
        assert install_spy.call_count == 1
        wrapped_once = memory_tool.memory_tool
        assert getattr(wrapped_once, _SHIM_MARKER, False)

        store = memory_tool.MemoryStore()
        accepted = json.loads(
            executor.execute_memory(
                _Agent(store),
                {
                    "action": "add",
                    "target": "memory",
                    "content": "The production database uses PostgreSQL 16 with pgvector.",
                },
            )
        )
        rejected = json.loads(
            executor.execute_memory(
                _Agent(store),
                {"action": "add", "target": "user", "content": "currently editing line 5"},
            )
        )
        await asyncio.sleep(0)

        assert accepted["provider"] == "engram"
        assert accepted["native_write"] is False
        assert rejected["success"] is False
        assert rejected["native_write"] is False
        assert first_submissions == [
            "The production database uses PostgreSQL 16 with pgvector."
        ]
        assert store.entries == {"memory": [], "user": []}

        second_provider = memory_plugins.load_memory_provider("engram_memory")
        assert second_provider is not None
        second_submissions: list[str] = []

        async def second_remember(
            content: str, metadata: dict[str, Any] | None = None
        ) -> None:
            del metadata
            second_submissions.append(content)

        second_provider._async_remember = second_remember
        second_provider.initialize("session-two", agent_context="primary")
        assert memory_tool.memory_tool is wrapped_once
        second_provider.initialize("session-three", agent_context="primary")
        assert memory_tool.memory_tool is wrapped_once
        second_provider.on_session_switch("session-four")

        executor.execute_memory(
            _Agent(store),
            {
                "action": "add",
                "target": "memory",
                "content": "The newest provider owns durable writes after session rotation.",
            },
        )
        await asyncio.sleep(0)

    assert install_spy.call_count == 3
    assert first_submissions == ["The production database uses PostgreSQL 16 with pgvector."]
    assert second_submissions == [
        "The newest provider owns durable writes after session rotation."
    ]
    assert second_provider._session_id == "session-four"
    assert store.entries == {"memory": [], "user": []}


def test_required_activation_failure_is_deferred_until_initialize(
    pinned_runtime: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    memory_plugins = pinned_runtime["plugins"]
    memory_tool = pinned_runtime["memory_tool"]

    discovered = memory_plugins.discover_memory_providers()
    provider = memory_plugins.load_memory_provider("engram_memory")
    pinned_runtime["status"].cmd_status(None)
    status_output = capsys.readouterr().out

    assert any(name == "engram_memory" and available for name, _, available in discovered)
    assert provider is not None
    assert "Plugin:    installed ✓" in status_output
    assert "Status:    available ✓" in status_output
    assert provider._activation_mode == "uninitialized"

    del memory_tool.memory_tool
    with pytest.raises(AutomaticCaptureUnavailable) as excinfo:
        provider.initialize("drifted-session", agent_context="primary")

    assert "tools.memory_tool.memory_tool" in str(excinfo.value)
    assert provider._activation_mode == "incompatible"
    assert provider._initialized is False

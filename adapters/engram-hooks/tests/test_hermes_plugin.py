"""Tests for the EngramMemoryProvider Hermes plugin.

Tests verify:
1. ABC conformance — all abstract methods implemented, instantiation succeeds.
2. prepare_memory_write routing — allows durable content, rejects ephemeral.
3. _async_remember — SDK attribute access (Pydantic), not dict access.
4. is_available — checks env/config without network calls.

These tests do NOT require a live Engram instance or a real Hermes installation.
A stub ABC mirroring Hermes' MemoryProvider is installed per-test.
"""
from __future__ import annotations

import abc
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make the plugin directory importable
_PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "..", "hermes_plugin")
sys.path.insert(0, os.path.abspath(_PLUGIN_DIR))


# ---------------------------------------------------------------------------
# Stub MemoryProvider ABC — mirrors the real Hermes contract
# ---------------------------------------------------------------------------


def _make_memory_provider_abc() -> type:
    """Build a real ABC with the same abstract methods as Hermes' MemoryProvider."""

    class MemoryProvider(abc.ABC):
        @property
        @abc.abstractmethod
        def name(self) -> str:
            ...

        @abc.abstractmethod
        def is_available(self) -> bool:
            ...

        @abc.abstractmethod
        def initialize(self, session_id: str, **kwargs: Any) -> None:
            ...

        @abc.abstractmethod
        def get_tool_schemas(self) -> list[dict[str, Any]]:
            ...

        # Non-abstract optional hooks (defaults match the real ABC)
        def prepare_memory_write(
            self,
            action: str,
            target: str,
            content: str,
            metadata: dict[str, Any] | None = None,
            old_text: str | None = None,
        ) -> dict[str, Any] | None:
            return None

        def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
            return ""

        def on_session_end(self, messages: list[dict[str, Any]]) -> None:  # noqa: B027
            pass

        def on_memory_write(  # noqa: B027
            self,
            action: str,
            target: str,
            content: str,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            pass

    return MemoryProvider


def _install_agent_stub() -> type:
    """Inject a stub agent.memory_provider module with a real ABC.

    Returns the MemoryProvider class. Must be called inside each test/fixture
    after clean_hooks_state has popped prior fake modules.
    """
    import types

    MemoryProvider = _make_memory_provider_abc()
    mod = types.ModuleType("agent.memory_provider")
    mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]
    parent = types.ModuleType("agent")
    parent.memory_provider = mod  # type: ignore[attr-defined]
    sys.modules["agent"] = parent
    sys.modules["agent.memory_provider"] = mod
    return MemoryProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    """Create an EngramMemoryProvider instance with mocked engram_hooks."""
    # Install the ABC stub BEFORE importing the plugin
    _install_agent_stub()

    # Mock engram_hooks before the plugin imports it in __init__
    mock_pkg = MagicMock()
    config_instance = MagicMock()
    config_instance.base_url = "https://engram.example.com"
    config_instance.api_key = "eng_test_key"
    mock_pkg.HooksConfig.return_value = config_instance

    hooks_instance = MagicMock()
    mock_pkg.LifecycleHooks.return_value = hooks_instance

    status = MagicMock()
    status.native_hook_available = True
    status.compat_shim_installed = False
    mock_pkg.install.return_value = {
        "hooks": hooks_instance,
        "status": status,
        "shim": status,
    }

    # Use the real guard implementation for accurate routing tests
    from engram_hooks.guards import (
        is_allowed as real_is_allowed,
    )
    from engram_hooks.guards import (
        prepare_memory_write_guard as real_guard,
    )

    mock_pkg.prepare_memory_write_guard = real_guard
    mock_pkg.is_allowed = real_is_allowed

    # Clear cached imports so the plugin re-imports with our stubs
    for mod_name in ("engram_memory", "engram_hooks"):
        sys.modules.pop(mod_name, None)

    with patch.dict(sys.modules, {"engram_hooks": mock_pkg}):
        from engram_memory import EngramMemoryProvider

        p = EngramMemoryProvider()
        p._hooks = hooks_instance
        yield p


# ---------------------------------------------------------------------------
# Tests: ABC conformance
# ---------------------------------------------------------------------------


class TestABCConformance:
    """Verify the plugin satisfies the MemoryProvider ABC contract.

    The original bug: EngramMemoryProvider was missing name, is_available,
    initialize, and get_tool_schemas, so Python refused to instantiate it.
    """

    def test_instantiation_succeeds(self, provider):
        assert provider is not None

    def test_name_property(self, provider):
        assert provider.name == "engram_memory"

    def test_is_available_true_with_config(self, provider):
        assert provider.is_available() is True

    def test_initialize_stores_session_id(self, provider):
        provider.initialize("test-session-123", agent_context="primary")
        assert provider._session_id == "test-session-123"

    def test_get_tool_schemas_returns_empty(self, provider):
        assert provider.get_tool_schemas() == []

    def test_install_result_parsed_correctly(self, provider):
        """install() returns a dict — the old code accessed .native_hook_available on it."""
        assert provider._native_hook is True
        assert provider._compat_shim is False


# ---------------------------------------------------------------------------
# Tests: prepare_memory_write routing
# ---------------------------------------------------------------------------


class TestPrepareMemoryWrite:
    """Verify the write-boundary guard routing in prepare_memory_write."""

    def test_durable_content_is_handled(self, provider):
        """Substantial durable content should be routed to Engram."""
        result = provider.prepare_memory_write(
            action="add",
            target="memory",
            content="The database uses PostgreSQL 16 with pgvector for vector search",
        )
        assert result is not None
        assert result["handled"] is True
        assert "Stored in Engram" in result["result"]

    def test_short_content_is_rejected(self, provider):
        """Content below the minimum length threshold should be rejected."""
        result = provider.prepare_memory_write(
            action="add",
            target="memory",
            content="hi",
        )
        assert result is not None
        assert result["handled"] is True
        assert "Rejected" in result["result"]

    def test_empty_content_is_rejected(self, provider):
        result = provider.prepare_memory_write(
            action="add",
            target="memory",
            content="",
        )
        assert result is not None
        assert result["handled"] is True
        assert "Rejected" in result["result"]

    def test_ephemeral_content_is_rejected(self, provider):
        """Ephemeral patterns (cursor position, 'now editing') should be rejected."""
        result = provider.prepare_memory_write(
            action="add",
            target="memory",
            content="currently editing the main configuration file",
        )
        assert result is not None
        assert result["handled"] is True
        assert "Rejected" in result["result"]

    def test_non_add_action_passthrough(self, provider):
        """replace/remove actions should not be intercepted."""
        result = provider.prepare_memory_write(
            action="replace",
            target="memory",
            content="Some durable content that would normally pass",
            old_text="old content",
        )
        assert result is None

    def test_non_memory_target_passthrough(self, provider):
        """Non-memory targets should not be intercepted."""
        result = provider.prepare_memory_write(
            action="add",
            target="somewhere_else",
            content="Some durable content that would normally pass",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _async_remember uses Pydantic attribute access
# ---------------------------------------------------------------------------


class TestAsyncRemember:
    """Verify _async_remember uses attribute access (Pydantic), not dict access."""

    @pytest.mark.asyncio
    async def test_successful_remember_uses_attribute_access(self, provider):
        """The old code called result.get('id') on a Pydantic model."""
        mock_client = AsyncMock()
        mock_result = MagicMock()
        mock_result.id = "item_123"
        mock_result.review_status = "proposed"
        mock_client.remember = AsyncMock(return_value=mock_result)

        provider._hooks._get_client.return_value = mock_client

        # Should not raise — uses getattr(), not dict access
        await provider._async_remember("test content")

        mock_client.remember.assert_called_once_with(
            content="test content",
            source_type="sync_turn",
        )

    @pytest.mark.asyncio
    async def test_remember_client_none_is_safe(self, provider):
        """When Engram client is unavailable, should log warning and return."""
        provider._hooks._get_client.return_value = None
        await provider._async_remember("test content")  # should not raise

    @pytest.mark.asyncio
    async def test_remember_exception_is_caught(self, provider):
        """SDK exceptions should be caught and logged, not propagated."""
        mock_client = AsyncMock()
        mock_client.remember = AsyncMock(side_effect=Exception("Network error"))
        provider._hooks._get_client.return_value = mock_client

        await provider._async_remember("test content")  # should not raise

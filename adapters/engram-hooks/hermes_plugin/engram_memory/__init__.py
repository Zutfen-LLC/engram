"""Hermes memory provider plugin backed by Engram.

Wraps the engram-hooks companion library as a Hermes ``MemoryProvider`` so that
all lifecycle hooks (``prepare_memory_write``, ``on_pre_compress``,
``on_session_end``) and the write-boundary guard route through Engram's
classify + remember pipeline.

Configuration is via ``ENGRAM_*`` env vars (see
:class:`engram_hooks.config.HooksConfig`). Install by copying this directory
to ``~/.hermes/plugins/engram_memory/`` and setting
``memory.provider: engram_memory`` in the profile's ``config.yaml``.
"""
from __future__ import annotations

import logging
from typing import Any

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


class EngramMemoryProvider(MemoryProvider):
    """Memory provider that routes candidates through engram-hooks to Engram.

    Implements the full ``MemoryProvider`` ABC contract:

    - ``name`` / ``is_available`` / ``initialize`` / ``get_tool_schemas``
      satisfy the abstract methods required for instantiation.
    - ``prepare_memory_write`` intercepts writes to ``memory``/``user``
      targets, routing them to Engram as ``sync_turn`` source items.
    - ``on_pre_compress`` and ``on_session_end`` extract durable facts
      through the engram-hooks lifecycle engine.
    - ``on_memory_write`` is a no-op — ``prepare_memory_write`` handles
      interception, so native writes that *do* go through are not mirrored.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from engram_hooks import HooksConfig, LifecycleHooks, install

        self._config = HooksConfig()
        self._hooks = LifecycleHooks(self._config)
        self._session_id: str = ""

        # Install the compat shim / native hook detection.
        try:
            self._install_result = install(config=self._config)
            status = self._install_result.get("status")
            self._native_hook = status.native_hook_available if status else False
            self._compat_shim = status.compat_shim_installed if status else False
        except Exception as e:
            logger.error(f"engram-hooks install() failed: {e}")
            self._install_result = None
            self._native_hook = False
            self._compat_shim = False

        logger.info(
            "EngramMemoryProvider initialized: base_url=%s, "
            "native_hook=%s, compat_shim=%s",
            getattr(self._config, "base_url", None) or "(unset)",
            self._native_hook,
            self._compat_shim,
        )

    # ---- ABC required methods ----

    @property
    def name(self) -> str:
        """Short identifier for this provider."""
        return "engram_memory"

    def is_available(self) -> bool:
        """True if Engram base URL and API key are configured.

        Does not make network calls — just checks config and env vars.
        """
        import os

        base_url = getattr(self._config, "base_url", None) or os.environ.get(
            "ENGRAM_BASE_URL", ""
        )
        api_key = getattr(self._config, "api_key", None) or os.environ.get(
            "ENGRAM_API_KEY", ""
        )
        return bool(base_url and api_key)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Initialize for a session.

        Called once at agent startup. Stores the session ID and logs the
        activation path.
        """
        self._session_id = session_id
        agent_context = kwargs.get("agent_context", "primary")
        logger.info(
            "EngramMemoryProvider.initialize: session=%s context=%s",
            session_id,
            agent_context,
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Engram exposes no extra agent tools — lifecycle is hook-driven."""
        return []

    # ---- prepare_memory_write: the write-boundary guard ----

    def prepare_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        old_text: str | None = None,
    ) -> dict[str, Any] | None:
        """Pre-commit guard: route durable writes to Engram.

        Returns ``{"handled": True, "result": ...}`` for ``memory``/``user``
        ``add`` actions whose content passes the write-boundary guard, to
        intercept the native write. Returns ``None`` for rejected content or
        non-add/non-memory actions so native storage proceeds normally.
        """
        import asyncio

        from engram_hooks import is_allowed, prepare_memory_write_guard

        # Only intercept add operations on memory/user targets.
        if target not in ("memory", "user") or action != "add":
            return None

        # Run the content guard — rejects ephemeral/ambiguous/too-short content.
        verdict = prepare_memory_write_guard(content)
        if not is_allowed(verdict):
            logger.debug(
                "engram-hooks guard rejected write: %s", verdict.get("reason")
            )
            # Rejection means the content is not durable — block the native
            # write too, since Engram's guard is the authority on quality.
            return {
                "handled": True,
                "result": f"Rejected by Engram guard: {verdict.get('reason')}",
            }

        # Content passed the guard — route to Engram instead of native store.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._async_remember(content, metadata))
            else:
                loop.run_until_complete(self._async_remember(content, metadata))
        except RuntimeError:
            asyncio.run(self._async_remember(content, metadata))

        return {
            "handled": True,
            "result": f"Stored in Engram: {content[:80]}{'...' if len(content) > 80 else ''}",
        }

    async def _async_remember(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Write a memory item to Engram via the SDK."""
        client = self._hooks._get_client()
        if client is None:
            logger.warning("Engram client unavailable — memory not stored")
            return

        try:
            result = await client.remember(
                content=content,
                source_type="sync_turn",
            )
            # SDK returns Pydantic models — use attribute access
            item_id = getattr(result, "id", None) or "?"
            review_status = getattr(result, "review_status", "?")
            logger.info(
                "Engram remember OK: id=%s review_status=%s", item_id, review_status
            )
        except Exception as e:
            logger.error("Engram remember failed: %s", e)

    # ---- Lifecycle hooks ----

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Extract durable facts before context compression."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._hooks.pre_compress(messages))
            else:
                loop.run_until_complete(self._hooks.pre_compress(messages))
        except RuntimeError:
            asyncio.run(self._hooks.pre_compress(messages))
        except Exception as e:
            logger.error("engram-hooks pre_compress failed: %s", e)

        return ""  # no injection into compression summary

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Extract durable facts at session end."""
        import asyncio

        try:
            asyncio.run(self._hooks.session_end(messages))
        except Exception as e:
            logger.error("engram-hooks session_end failed: %s", e)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mirror native memory writes — no-op (prepare_memory_write handles it)."""
        pass

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
        import threading

        from engram_hooks import HooksConfig, LifecycleHooks, install

        self._config = HooksConfig()
        self._hooks = LifecycleHooks(self._config)
        self._session_id: str = ""

        # Prefetch cache: queue_prefetch writes results here in a background
        # thread, prefetch() reads (and clears) them on the next turn.
        self._prefetch_lock = threading.Lock()
        self._prefetch_result: str = ""
        self._startup_recall_done = False

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

        Called once at agent startup. Stores the session ID, resets the
        per-session context accumulator so prior-session context doesn't
        bleed in, fires startup recall, and logs the activation path.
        """
        self._session_id = session_id
        self._hooks.reset_session_context()
        agent_context = kwargs.get("agent_context", "primary")
        logger.info(
            "EngramMemoryProvider.initialize: session=%s context=%s",
            session_id,
            agent_context,
        )
        # Fire startup recall in a background thread so it populates the
        # prefetch cache before the first turn.
        if not self._startup_recall_done:
            self._startup_recall_done = True
            self._do_startup_recall()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Engram exposes no extra agent tools — lifecycle is hook-driven."""
        return []

    # ---- Recall: system_prompt_block + prefetch ----

    def system_prompt_block(self) -> str:
        """Static block for the system prompt."""
        return (
            "# Engram Memory\n"
            "Active. Memories are stored in and recalled from the Engram\n"
            "memory service. Relevant context is injected automatically before\n"
            "each turn via the prefetch mechanism."
        )

    def _do_startup_recall(self) -> None:
        """Fetch startup memories in a background thread, store in prefetch cache."""
        import threading

        def _bg_recall() -> None:
            try:
                result = self._run_async(self._async_startup_recall())
                if result:
                    with self._prefetch_lock:
                        self._prefetch_result = result
                    logger.info(
                        "Engram startup recall populated prefetch cache (%d chars)",
                        len(result),
                    )
            except Exception as e:
                logger.debug("Engram startup recall failed: %s", e)

        threading.Thread(target=_bg_recall, daemon=True).start()

    async def _async_startup_recall(self) -> str:
        """Call /v1/recall mode=startup and format results as a context block."""
        client = self._hooks._get_client()
        if client is None:
            return ""
        try:
            resp = await client.recall(mode="startup", item_budget=5)
            if not resp.items:
                return ""
            lines = ["## Engram Startup Recall"]
            for item in resp.items:
                content = item.get("content", "")
                wing = item.get("wing", "")
                room = item.get("room", "")
                tag = f"[{wing}/{room}]" if wing or room else ""
                lines.append(f"- {tag} {content[:200]}".strip())
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Engram startup recall API error: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire a background semantic search for the upcoming turn.

        Called after each turn. The result populates _prefetch_result,
        consumed by prefetch() on the next turn.
        """
        if not query or len(query.strip()) < 3:
            return
        import threading

        def _bg_search() -> None:
            try:
                result = self._run_async(self._async_search(query))
                if result:
                    with self._prefetch_lock:
                        self._prefetch_result = result
                    logger.debug(
                        "Engram prefetch populated for query (%d chars)", len(result)
                    )
            except Exception as e:
                logger.debug("Engram prefetch failed: %s", e)

        threading.Thread(target=_bg_search, daemon=True).start()

    async def _async_search(self, query: str) -> str:
        """Call /v1/search and format results as a context block."""
        client = self._hooks._get_client()
        if client is None:
            return ""
        try:
            resp = await client.search(query, mode="hybrid", limit=5)
            if not resp.results:
                return ""
            lines = ["## Engram Relevant Memories"]
            for r in resp.results:
                content = r.get("content", "")
                score = r.get("score", 0)
                wing = r.get("wing", "")
                room = r.get("room", "")
                tag = f"[{wing}/{room}]" if wing or room else ""
                lines.append(f"- {tag} {content[:200]}".strip())
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Engram search API error: %s", e)
            return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached recall results from the previous background fetch.

        Called before each API call. Returns the result of the startup
        recall or the previous turn's queue_prefetch search, then clears
        the cache so it's only injected once.
        """
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        return result

    @staticmethod
    def _run_async(coro: Any) -> Any:
        """Run a coroutine from a sync context (background thread).

        Uses a fresh event loop — safe because this runs in a daemon thread
        that has no existing loop.
        """
        import asyncio

        return asyncio.run(coro)

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
        # Always fire-and-forget: the tool return value is already set below.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._async_remember(content, metadata))
            else:
                # No running loop — run in a daemon thread, don't block.
                import threading

                def _bg_remember() -> None:
                    try:
                        asyncio.run(self._async_remember(content, metadata))
                    except Exception as e:
                        logger.debug("engram remember failed (background): %s", e)

                threading.Thread(target=_bg_remember, daemon=True).start()
        except RuntimeError:
            import threading

            def _bg_remember_re() -> None:
                try:
                    asyncio.run(self._async_remember(content, metadata))
                except Exception as e:
                    logger.debug("engram remember failed (background): %s", e)

            threading.Thread(target=_bg_remember_re, daemon=True).start()

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
        """Extract durable facts before context compression.

        Same non-blocking pattern as ``on_session_end``: fire-and-forget
        on a running loop, bounded-time on a stopped loop, daemon-thread
        fallback with no loop. Never block compression — or worse, the
        conversation loop — on HTTP round-trips to Engram.
        """
        import asyncio
        import threading

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._hooks.pre_compress(messages))
            else:
                loop.run_until_complete(
                    asyncio.wait_for(
                        self._hooks.pre_compress(messages),
                        timeout=5.0,
                    )
                )
        except RuntimeError:
            def _bg_pre_compress() -> None:
                try:
                    asyncio.run(
                        asyncio.wait_for(
                            self._hooks.pre_compress(messages),
                            timeout=10.0,
                        )
                    )
                except TimeoutError:
                    logger.debug("engram-hooks pre_compress timed out (background)")
                except Exception as e:
                    logger.debug("engram-hooks pre_compress failed (background): %s", e)

            t = threading.Thread(target=_bg_pre_compress, daemon=True)
            t.start()
        except TimeoutError:
            logger.debug("engram-hooks pre_compress timed out")
        except Exception as e:
            logger.error("engram-hooks pre_compress failed: %s", e)

        return ""  # no injection into compression summary

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Extract durable facts at session end — fire-and-forget, never block.

        Session end is called during /new, /quit, and CLI shutdown. Blocking
        here freezes the CLI for the duration of the extraction, which can
        take minutes for long sessions (each candidate requires an HTTP
        round-trip to Engram for classify + remember). Instead we schedule
        the extraction as a background task and return immediately.

        If there's no running event loop (e.g. shutdown path), we run in a
        daemon thread with a hard timeout so we never block exit.
        """
        import asyncio
        import threading

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Fire-and-forget on the existing loop.
                asyncio.ensure_future(self._hooks.session_end(messages))
            else:
                # Loop exists but not running — run with a timeout so we
                # don't block indefinitely on hundreds of HTTP calls.
                loop.run_until_complete(
                    asyncio.wait_for(
                        self._hooks.session_end(messages),
                        timeout=5.0,
                    )
                )
        except RuntimeError:
            # No event loop in this thread — run in a daemon thread so it
            # doesn't block exit. The process may terminate before it
            # completes, which is acceptable for session-end extraction.
            def _bg_session_end() -> None:
                try:
                    asyncio.run(
                        asyncio.wait_for(
                            self._hooks.session_end(messages),
                            timeout=10.0,
                        )
                    )
                except TimeoutError:
                    logger.debug("engram-hooks session_end timed out (background)")
                except Exception as e:
                    logger.debug("engram-hooks session_end failed (background): %s", e)

            t = threading.Thread(target=_bg_session_end, daemon=True)
            t.start()
        except TimeoutError:
            logger.debug("engram-hooks session_end timed out")
        except Exception as e:
            logger.error("engram-hooks session_end failed: %s", e)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Extract durable facts from the completed turn — fire-and-forget.

        Called after every turn. The assistant's response is where durable
        facts live (decisions, conclusions, findings), so we extract from
        ``assistant_content``. ``user_content`` provides context but is rarely
        a source of durable facts itself.

        Non-blocking: schedules on the running loop or a daemon thread, same
        pattern as ``on_session_end`` and ``on_pre_compress``.
        """
        import asyncio
        import threading

        # Skip empty or trivially short turns.
        if not assistant_content or len(assistant_content.strip()) < 20:
            return

        payload = assistant_content

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._hooks.sync_turn(payload))
            else:
                loop.run_until_complete(
                    asyncio.wait_for(
                        self._hooks.sync_turn(payload),
                        timeout=5.0,
                    )
                )
        except RuntimeError:
            def _bg_sync_turn() -> None:
                try:
                    asyncio.run(
                        asyncio.wait_for(
                            self._hooks.sync_turn(payload),
                            timeout=10.0,
                        )
                    )
                except TimeoutError:
                    logger.debug("engram-hooks sync_turn timed out (background)")
                except Exception as e:
                    logger.debug("engram-hooks sync_turn failed (background): %s", e)

            t = threading.Thread(target=_bg_sync_turn, daemon=True)
            t.start()
        except TimeoutError:
            logger.debug("engram-hooks sync_turn timed out")
        except Exception as e:
            logger.error("engram-hooks sync_turn failed: %s", e)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mirror native memory writes — no-op (prepare_memory_write handles it)."""
        pass

"""Faithful selected-provider initialization shape from pinned Hermes."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MemoryManager:
    """Minimal manager preserving stock ``initialize_all`` exception handling."""

    def __init__(self) -> None:
        self._providers: list[Any] = []
        self.notifications: list[tuple[Any, dict[str, Any]]] = []

    @property
    def providers(self) -> list[Any]:
        return list(self._providers)

    def add_provider(self, provider: Any) -> None:
        self._providers.append(provider)

    def initialize_all(self, session_id: str, **kwargs: Any) -> None:
        """Initialize every provider while swallowing each provider failure."""
        if "hermes_home" not in kwargs:
            from hermes_constants import get_hermes_home

            kwargs["hermes_home"] = str(get_hermes_home())
        for provider in self._providers:
            try:
                provider.initialize(session_id=session_id, **kwargs)
            except Exception as exc:
                logger.warning(
                    "Memory provider '%s' initialize failed: %s",
                    provider.name,
                    exc,
                )

    def notify_memory_tool_write(
        self,
        result: Any,
        args: dict[str, Any],
        *,
        build_metadata: Any = None,
    ) -> None:
        del build_metadata
        self.notifications.append((result, args))

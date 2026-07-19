"""Compatibility-relevant ABC extraction from stock agent/memory_provider.py."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """Stock revision has on_memory_write but no prepare_memory_write hook."""

    def __init__(self, **kwargs: Any) -> None:
        del kwargs

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider."""

    @abstractmethod
    def is_available(self) -> bool:
        """Perform only a local configuration/dependency check."""

    @abstractmethod
    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Initialize the selected provider at agent startup."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return provider tool schemas."""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        del action, target, content, metadata

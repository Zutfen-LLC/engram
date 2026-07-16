"""Compatibility-relevant subset of stock agent/memory_provider.py."""


class MemoryProvider:
    """Stock revision has on_memory_write but no prepare_memory_write hook."""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        del action, target, content, metadata

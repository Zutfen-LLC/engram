"""Minimal pinned agent-init path for the configured memory provider."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def initialize_memory_provider(
    agent: Any,
    provider_name: str,
    *,
    platform: str = "cli",
) -> None:
    """Load, register, and initialize the selected provider as stock Hermes does."""
    agent._memory_manager = None
    try:
        from plugins.memory import load_memory_provider

        from agent.memory_manager import MemoryManager

        manager = MemoryManager()
        provider = load_memory_provider(provider_name)
        if provider and provider.is_available():
            manager.add_provider(provider)
        if manager.providers:
            manager.initialize_all(
                session_id=agent.session_id,
                platform=platform,
                agent_context="primary",
            )
            logger.info("Memory provider '%s' activated", provider_name)
            agent._memory_manager = manager
        else:
            agent._memory_manager = None
    except Exception as exc:
        logger.warning("Memory provider plugin init failed: %s", exc)
        agent._memory_manager = None

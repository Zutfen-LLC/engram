"""Typed provider configuration shared by classification call sites."""

from __future__ import annotations

from dataclasses import dataclass

from engram.config import Settings, settings
from engram.usage import safe_provider_identity


@dataclass(frozen=True)
class ClassificationProviderConfig:
    """Resolved OpenAI-compatible classification provider configuration."""

    provider_adapter: str
    api_key: str | None
    base_url: str | None
    model: str
    sanitized_provider_host: str | None


def resolve_classification_provider(
    source: Settings = settings,
) -> ClassificationProviderConfig:
    """Resolve the one configuration contract used by all classification paths."""
    adapter = source.classification_provider or "none"
    api_key = source.classification_api_key or source.openai_api_key
    base_url = source.classification_base_url or source.openai_base_url
    _, host = safe_provider_identity(adapter, base_url)
    return ClassificationProviderConfig(
        provider_adapter=adapter,
        api_key=api_key,
        base_url=base_url,
        model=source.classification_model,
        sanitized_provider_host=host,
    )

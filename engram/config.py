"""Configuration management for Engram service."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGRAM_",
        env_file=".env",
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://engram:engram@localhost:5432/engram"

    # Service
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Embeddings
    embedding_dim: int = 1536
    # Provider: "openai", "local", or "none" (defer embedding generation)
    embedding_provider: str = "none"
    openai_api_key: str | None = None

    # Classification
    # Provider: "openai", "local", or "none" (rule-based fallback only)
    classification_provider: str = "none"
    classification_model: str = "gpt-4o-mini"
    classification_confidence_threshold: float = 0.5

    # Auth
    # When false, auth is skipped (dev mode). Production must set True.
    auth_enabled: bool = False

    # Recall defaults
    recall_byte_budget: int = 4096
    recall_item_budget: int = 50
    max_pinned_tokens: int = 2048             # hard ceiling for pinned items in startup recall
    stale_after_days: int = 90                # items not recalled in N days are "stale"
    startup_recall_penalty_threshold: int = 5  # penalize after N startup recalls without feedback
    startup_recall_penalty_factor: float = 0.5  # reduce recency bonus by this per excess recall


settings = Settings()

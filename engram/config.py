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

    # Auth
    # When false, auth is skipped (dev mode). Production must set True.
    auth_enabled: bool = False

    # Recall defaults
    recall_byte_budget: int = 4096
    recall_item_budget: int = 50


settings = Settings()

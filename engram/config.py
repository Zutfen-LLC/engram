"""Configuration management for Engram service."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGRAM_",
        env_file=".env",
        extra="ignore",
    )

    # Database — runtime application role (non-owner; RLS-enforced).
    database_url: str = "postgresql+asyncpg://engram:engram@localhost:5432/engram"

    # Owner/migration role — used for DDL (``engram init-db``), the first-key
    # bootstrap, cross-tenant admin/CLI scans, and principal/key resolution
    # (which must see across tenants and therefore bypass RLS). When unset it
    # falls back to ``database_url`` (single-role dev/test, where the same role
    # is both owner and app). In the default Compose deployment this points at
    # the table-owning superuser so migrations and admin commands work.
    owner_database_url: str | None = None

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

    # Short TTL (seconds) for the in-process principal cache used by new-format
    # (eng_<key_id>_<secret>) API keys. A successful verification is cached so
    # repeated requests with the same key skip the DB lookup. Set to 0 to
    # disable caching. Revocation takes effect after at most this many seconds
    # (a revoked key may still authenticate until its cache entry expires).
    api_key_cache_ttl_seconds: int = 60
    # Safety cap on the number of cached principals (per process). Evicts the
    # soonest-expiring entries when exceeded.
    api_key_cache_max_size: int = 4096

    # Recall defaults
    recall_byte_budget: int = 4096
    recall_item_budget: int = 50
    max_pinned_tokens: int = 2048  # hard ceiling for pinned items in startup recall
    stale_after_days: int = 90  # items not verified in N days are "stale"
    # penalize after N startup recalls without feedback
    startup_recall_penalty_threshold: int = 5
    startup_recall_penalty_factor: float = 0.5  # reduce recency bonus per excess recall
    startup_recall_penalty_floor: float = 0.1  # recency component minimum (never zero)
    # N distinct non-author agents for partial penalty reset
    quorum_reset_agent_count: int = 2

    # Write-path cost control
    # if False, low-trust writes defer conflict check to promotion
    conflict_check_on_write: bool = True


settings = Settings()

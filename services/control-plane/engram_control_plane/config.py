"""Configuration for the Engram Control Plane.

Settings load at import time (pydantic-settings reads the environment), but
production-required validation — e.g. that a database URL is configured when
the environment is labeled production — runs at command execution / app
startup (see :func:`validate_for_runtime`), NOT at module import. This keeps
app import and ``export-openapi`` database-free and secret-free: importing
``engram_control_plane.api.app`` must never touch the database or require
production secrets to be present (ENG-PORTAL-001A, alteration 6).
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Control Plane settings, sourced from ``ENGRAM_CONTROL_*`` environment.

    All defaults are non-secret and safe for local development. A deployment
    that labels ``environment="production"`` must additionally pass
    :func:`validate_for_runtime` at startup.
    """

    model_config = SettingsConfigDict(
        env_prefix="ENGRAM_CONTROL_",
        env_file=".env",
        extra="ignore",
    )

    # Deployment identity.
    # "development" / "staging" / "production". Production-labeled startup is
    # held to stricter required-configuration rules (validate_for_runtime).
    environment: Literal["development", "staging", "production"] = "development"
    host: str = "0.0.0.0"
    port: int = 8100
    log_level: str = "info"

    # Dedicated Control Plane database (SQLAlchemy async URL, asyncpg dialect).
    # The runtime role (engram_control_app) connects here; migrations use the
    # owner/migration URL below. None by default — local dev that does not need
    # readiness may run without a database, and export-openapi/health stay DB-free.
    database_url: str | None = None
    owner_database_url: str | None = None

    # Build provenance. Populated by the container image; "unknown" is the safe
    # default so a source checkout remains importable without build metadata.
    build_sha: str = "unknown"

    @model_validator(mode="after")
    def _normalize_log_level(self) -> Settings:
        level = (self.log_level or "info").strip().lower()
        if not level:
            level = "info"
        self.log_level = level
        return self

    def is_production_like(self) -> bool:
        """True when the environment label demands production-grade config."""
        return self.environment in ("production", "staging")


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or unsafe.

    This is raised by :func:`validate_for_runtime`, not at settings import.
    """


def validate_for_runtime(settings: Settings) -> None:
    """Validate configuration required to *run* the service.

    Invoked from the CLI (``serve``) and the app lifespan — i.e. at command
    execution / startup, never at module import. In a production-labeled
    environment a database URL is mandatory; in development it is optional so
    that ``export-openapi``, local scaffolding, and unit tests stay DB-free.
    """
    if settings.is_production_like():
        if not settings.database_url:
            raise ConfigurationError(
                "ENGRAM_CONTROL_DATABASE_URL is required in a "
                f"{settings.environment!r} environment."
            )
        if not settings.owner_database_url:
            raise ConfigurationError(
                "ENGRAM_CONTROL_OWNER_DATABASE_URL is required for migrations "
                f"in a {settings.environment!r} environment."
            )


settings = Settings()

"""Configuration validation tests.

Production-required validation runs at runtime (validate_for_runtime), NOT at
module import. App import is always database-free and secret-free.
"""

from __future__ import annotations

import importlib

import pytest


def test_app_import_is_database_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the app requires no database URL and no secrets."""
    for key in list(__import__("os").environ):
        if key.startswith("ENGRAM_CONTROL_"):
            monkeypatch.delenv(key, raising=False)
    import engram_control_plane.api.app as app_mod

    importlib.reload(app_mod)
    app = app_mod.create_app()
    # Health works without any database.
    assert app.title == "Engram Control Plane"


def test_production_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A production-labeled environment rejects startup with no DATABASE_URL."""
    import engram_control_plane.config as config_mod

    monkeypatch.setenv("ENGRAM_CONTROL_ENVIRONMENT", "production")
    # No database URL set.
    monkeypatch.delenv("ENGRAM_CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("ENGRAM_CONTROL_OWNER_DATABASE_URL", raising=False)
    importlib.reload(config_mod)
    s = config_mod.Settings()
    with pytest.raises(config_mod.ConfigurationError):
        config_mod.validate_for_runtime(s)


def test_production_requires_owner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production requires the owner/migration URL too."""
    import engram_control_plane.config as config_mod

    monkeypatch.setenv("ENGRAM_CONTROL_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ENGRAM_CONTROL_DATABASE_URL",
        "postgresql+asyncpg://u:p@h/engram_control",
    )
    monkeypatch.delenv("ENGRAM_CONTROL_OWNER_DATABASE_URL", raising=False)
    importlib.reload(config_mod)
    s = config_mod.Settings()
    with pytest.raises(config_mod.ConfigurationError):
        config_mod.validate_for_runtime(s)


def test_production_ok_with_both_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production startup passes when both URLs are present."""
    import engram_control_plane.config as config_mod

    monkeypatch.setenv("ENGRAM_CONTROL_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ENGRAM_CONTROL_DATABASE_URL",
        "postgresql+asyncpg://engram_control_app:x@h/engram_control",
    )
    monkeypatch.setenv(
        "ENGRAM_CONTROL_OWNER_DATABASE_URL",
        "postgresql+asyncpg://engram:x@h/engram_control",
    )
    importlib.reload(config_mod)
    s = config_mod.Settings()
    # No raise.
    config_mod.validate_for_runtime(s)


def test_development_does_not_require_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Development never requires a database (scaffold/export-openapi stay free)."""
    import engram_control_plane.config as config_mod

    monkeypatch.setenv("ENGRAM_CONTROL_ENVIRONMENT", "development")
    monkeypatch.delenv("ENGRAM_CONTROL_DATABASE_URL", raising=False)
    importlib.reload(config_mod)
    s = config_mod.Settings()
    config_mod.validate_for_runtime(s)  # no raise


def test_export_openapi_is_database_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """export-openapi succeeds with no database configured."""
    import json

    for key in list(__import__("os").environ):
        if key.startswith("ENGRAM_CONTROL_"):
            monkeypatch.delenv(key, raising=False)
    import engram_control_plane.api.app as app_mod
    import engram_control_plane.api.openapi as openapi_mod

    importlib.reload(app_mod)
    text = openapi_mod.canonical_openapi_json(app_mod.create_app())
    schema = json.loads(text)
    assert schema["info"]["title"] == "Engram Control Plane"
    assert "/health" in schema["paths"]
    assert "/ready" in schema["paths"]
    assert "/control/v1/meta" in schema["paths"]

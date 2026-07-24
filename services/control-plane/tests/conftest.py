"""Shared test fixtures for the Control Plane.

App-level unit tests (health, meta, config, security, openapi) run without a
database via httpx ASGITransport. The readiness/migration/role-isolation tests
that need a real PostgreSQL live in the CI Compose suite, not here.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests start from a non-production, database-free baseline.

    Remove any inherited production/DB env so import-time settings reflect the
    scaffold defaults, then re-import settings per test where needed.
    """
    for key in list(os.environ):
        if key.startswith("ENGRAM_CONTROL_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An httpx client bound to a fresh app instance (no DB required)."""
    # Import here so _clean_env has cleared the environment first.
    from engram_control_plane.api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch):
    """Re-load Settings from the current (monkeypatched) environment."""
    import importlib

    import engram_control_plane.config as config_mod

    importlib.reload(config_mod)
    return config_mod.settings

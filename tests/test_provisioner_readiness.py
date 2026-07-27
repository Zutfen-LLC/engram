"""Readiness checks for the least-privilege provisioner connection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app


def _role(**overrides: object) -> dict[str, object]:
    role: dict[str, object] = {
        "rolname": "engram_provisioner",
        "rolcanlogin": True,
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolinherit": False,
        "prohibited_membership": False,
        "schema_create": False,
    }
    role.update(overrides)
    return role


def _override_normal_ready_session(app) -> None:  # type: ignore[no-untyped-def]
    from engram.db import get_session

    async def execute(statement, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        result = MagicMock()
        if "current_setting" in str(statement):
            result.scalar.return_value = "tenant-1"
        else:
            result.scalar.return_value = "0.8.0"
        return result

    async def override_get_session():  # type: ignore[no-untyped-def]
        session = MagicMock()
        session.execute = AsyncMock(side_effect=execute)
        yield session

    app.dependency_overrides[get_session] = override_get_session


def _install_provisioner(monkeypatch, role: dict[str, object], *, tables: int = 6, function=True):  # type: ignore[no-untyped-def]
    import engram.db as db_module

    class ProvisionerSession:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        async def execute(self, statement):  # type: ignore[no-untyped-def]
            result = MagicMock()
            sql = str(statement)
            if "FROM pg_roles" in sql:
                result.mappings.return_value.first.return_value = role
            elif "information_schema.tables" in sql:
                result.scalar.return_value = tables
            else:
                result.scalar.return_value = function
            return result

    monkeypatch.setattr(
        db_module, "require_provisioner_session_factory", lambda: ProvisionerSession
    )


@pytest.mark.parametrize(
    ("override",),
    [
        ({"rolname": "wrong"},),
        ({"rolcanlogin": False},),
        ({"rolsuper": True},),
        ({"rolbypassrls": True},),
        ({"rolcreatedb": True},),
        ({"rolcreaterole": True},),
        ({"rolreplication": True},),
        ({"rolinherit": True},),
        ({"prohibited_membership": True},),
        ({"schema_create": True},),
    ],
)
async def test_ready_rejects_any_invalid_provisioner_posture(monkeypatch, override):  # type: ignore[no-untyped-def]
    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    app = create_app()
    _override_normal_ready_session(app)
    _install_provisioner(monkeypatch, _role(**override))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["provisioning"] == "misconfigured"


@pytest.mark.parametrize(("tables", "function"), [(5, True), (6, False)])
async def test_ready_rejects_missing_provisioning_objects(monkeypatch, tables, function):  # type: ignore[no-untyped-def]
    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    app = create_app()
    _override_normal_ready_session(app)
    _install_provisioner(monkeypatch, _role(), tables=tables, function=function)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["provisioning"] == "misconfigured"


async def test_ready_accepts_the_intended_provisioner_posture(monkeypatch) -> None:
    from engram.config import settings

    monkeypatch.setattr(settings, "service_provisioning_enabled", True)
    app = create_app()
    _override_normal_ready_session(app)
    _install_provisioner(monkeypatch, _role())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200

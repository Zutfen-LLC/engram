"""Metadata endpoint tests."""

from __future__ import annotations

from httpx import AsyncClient

from engram_control_plane.api.routes.meta import META_REQUIRED_FIELDS, META_REQUIRED_VALUES


async def test_meta_contains_only_documented_fields(client: AsyncClient) -> None:
    """The meta response exposes exactly the documented safe fields."""
    r = await client.get("/control/v1/meta")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == set(META_REQUIRED_FIELDS)


async def test_meta_required_values(client: AsyncClient) -> None:
    """The boundary contract values are fixed."""
    r = await client.get("/control/v1/meta")
    body = r.json()
    for key, value in META_REQUIRED_VALUES.items():
        assert body[key] == value, f"{key} should be {value!r}, got {body[key]!r}"


async def test_meta_has_service_metadata(client: AsyncClient) -> None:
    r = await client.get("/control/v1/meta")
    body = r.json()
    assert body["service"] == "engram-control-plane"
    assert body["environment"] in ("development", "staging", "production")
    assert "build_sha" in body
    assert "api_version" in body

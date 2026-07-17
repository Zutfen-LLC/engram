"""Real-database API and concurrency coverage for memory profiles."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
from engram.migrations import normalize_asyncpg_url


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")


async def _owner():
    import asyncpg

    if not _owner_dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")
    try:
        return await asyncpg.connect(normalize_asyncpg_url(_owner_dsn()))  # type: ignore[arg-type]
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")


def _profile_body(slug: str, *, policy: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "name": slug.title(),
        "slug": slug,
        "description": "test profile",
        "policy": policy or {},
        "reason": "initial policy",
    }


async def test_profile_policy_creation_tenant_isolation_and_no_membership_side_effect() -> None:
    owner = await _owner()
    slug = f"profile-{uuid.uuid4().hex[:10]}"
    created_ids: list[uuid.UUID] = []
    try:
        default = await owner.fetchrow(
            "SELECT t.id AS tenant_id, p.id AS principal_id "
            "FROM tenants t JOIN principals p ON p.tenant_id = t.id "
            "WHERE t.slug = 'default' AND p.name = 'admin'"
        )
        workspace = await owner.fetchrow(
            "SELECT id, slug FROM workspaces WHERE tenant_id = $1 ORDER BY created_at LIMIT 1",
            default["tenant_id"],
        )
        assert workspace is not None
        membership_before = await owner.fetchval(
            "SELECT count(*) FROM workspace_members WHERE principal_id = $1",
            default["principal_id"],
        )

        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            invalid = await client.post("/v1/memory-profiles", json=_profile_body("Invalid_Slug"))
            assert invalid.status_code == 422

            unknown = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"{slug}-unknown",
                    policy={
                        "workspace_grants": [
                            {
                                "workspace_id": str(uuid.uuid4()),
                                "can_read": True,
                                "can_write": False,
                            }
                        ]
                    },
                ),
            )
            assert unknown.status_code == 422

            policy = {
                "default_write_visibility": "workspace",
                "default_write_workspace_id": str(workspace["id"]),
                "workspace_grants": [
                    {
                        "workspace_id": str(workspace["id"]),
                        "can_read": True,
                        "can_write": True,
                    }
                ],
            }
            created = await client.post(
                "/v1/memory-profiles", json=_profile_body(slug, policy=policy)
            )
            assert created.status_code == 201, created.text
            body = created.json()
            created_ids.append(uuid.UUID(body["id"]))
            assert body["active_revision"]["version"] == 1
            assert body["active_revision"]["workspace_grants"] == [
                {
                    "workspace_id": str(workspace["id"]),
                    "workspace_slug": workspace["slug"],
                    "can_read": True,
                    "can_write": True,
                }
            ]

            duplicate = await client.post(
                "/v1/memory-profiles", json=_profile_body(slug, policy=policy)
            )
            assert duplicate.status_code == 409

        assert (
            await owner.fetchval(
                "SELECT count(*) FROM workspace_members WHERE principal_id = $1",
                default["principal_id"],
            )
            == membership_before
        )
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM memory_profile_events "
                "WHERE profile_id = $1 AND event_type = 'profile_created'",
                created_ids[0],
            )
            == 1
        )
    finally:
        if created_ids:
            await owner.execute(
                "DELETE FROM memory_profiles WHERE id = ANY($1::uuid[])", created_ids
            )
        await owner.close()


async def test_revision_concurrency_lifecycle_and_audit_are_atomic() -> None:
    owner = await _owner()
    slug = f"profile-{uuid.uuid4().hex[:10]}"
    profile_id: uuid.UUID | None = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            created = await client.post("/v1/memory-profiles", json=_profile_body(slug))
            assert created.status_code == 201, created.text
            profile_id = uuid.UUID(created.json()["id"])
            revision_v1 = created.json()["active_revision_id"]
            revision_request = {
                "expected_active_revision_id": revision_v1,
                "policy": {"include_private": True},
                "reason": "concurrent revision",
            }
            first, second = await asyncio.gather(
                client.post(
                    f"/v1/memory-profiles/{profile_id}/revisions",
                    json=revision_request,
                ),
                client.post(
                    f"/v1/memory-profiles/{profile_id}/revisions",
                    json=revision_request,
                ),
            )
            assert sorted((first.status_code, second.status_code)) == [200, 409]
            winner = first if first.status_code == 200 else second
            assert winner.json()["active_revision"]["version"] == 2
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM memory_profile_revisions WHERE profile_id = $1",
                    profile_id,
                )
                == 2
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM memory_profile_workspace_grants g "
                    "JOIN memory_profile_revisions r ON r.id = g.revision_id "
                    "WHERE r.profile_id = $1",
                    profile_id,
                )
                == 0
            )

            stale = await client.post(
                f"/v1/memory-profiles/{profile_id}/revisions", json=revision_request
            )
            assert stale.status_code == 409

            disabled = await client.post(
                f"/v1/memory-profiles/{profile_id}/disable", json={"reason": "pause"}
            )
            assert disabled.status_code == 200
            disabled_again = await client.post(
                f"/v1/memory-profiles/{profile_id}/disable", json={"reason": "pause again"}
            )
            assert disabled_again.status_code == 200
            rejected_revision = await client.post(
                f"/v1/memory-profiles/{profile_id}/revisions",
                json={
                    "expected_active_revision_id": winner.json()["active_revision_id"],
                    "policy": {},
                    "reason": "must fail",
                },
            )
            assert rejected_revision.status_code == 409
            enabled = await client.post(
                f"/v1/memory-profiles/{profile_id}/enable", json={"reason": "resume"}
            )
            assert enabled.status_code == 200
            enabled_again = await client.post(
                f"/v1/memory-profiles/{profile_id}/enable", json={"reason": "resume again"}
            )
            assert enabled_again.status_code == 200

            openapi = (await client.get("/openapi.json")).json()
            assert f"/v1/memory-profiles/{profile_id}" not in openapi["paths"]
            assert "/v1/memory-profiles/{profile_id}" in openapi["paths"]
            assert "delete" not in openapi["paths"]["/v1/memory-profiles/{profile_id}"]
            assert "patch" not in openapi["paths"]["/v1/memory-profiles/{profile_id}"]

        events = await owner.fetch(
            "SELECT event_type, details FROM memory_profile_events "
            "WHERE profile_id = $1 ORDER BY created_at",
            profile_id,
        )
        assert [event["event_type"] for event in events].count("revision_activated") == 1
        assert [event["event_type"] for event in events].count("profile_disabled") == 1
        assert [event["event_type"] for event in events].count("profile_enabled") == 1
        revision_event = next(
            event for event in events if event["event_type"] == "revision_activated"
        )
        details = revision_event["details"]
        if isinstance(details, str):
            details = json.loads(details)
        assert details == {
            "previous_active_revision_id": revision_v1,
            "new_active_revision_id": winner.json()["active_revision_id"],
            "version": 2,
            "workspace_grant_count": 0,
            "default_write_visibility": "private",
        }
    finally:
        if profile_id is not None:
            await owner.execute("DELETE FROM memory_profiles WHERE id = $1", profile_id)
        await owner.close()


async def test_profiled_agent_issuance_is_atomic_and_disabled_binding_creates_nothing() -> None:
    owner = await _owner()
    slug = f"agent-profile-{uuid.uuid4().hex[:10]}"
    profile_id: uuid.UUID | None = None
    agent_ids: list[uuid.UUID] = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            created = await client.post("/v1/memory-profiles", json=_profile_body(slug))
            assert created.status_code == 201, created.text
            profile_id = uuid.UUID(created.json()["id"])
            revision_id = created.json()["active_revision_id"]

            agent_name = f"profiled-agent-{uuid.uuid4().hex[:10]}"
            agent = await client.post(
                "/v1/agents",
                json={"name": agent_name, "memory_profile_id": str(profile_id)},
            )
            assert agent.status_code == 201, agent.text
            agent_ids.append(uuid.UUID(agent.json()["id"]))
            assert agent.json()["memory_profile_id"] == str(profile_id)
            assert agent.json()["memory_profile_revision_id"] == revision_id
            assert agent.json()["memory_profile_slug"] == slug
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM api_keys WHERE id = $1 AND memory_profile_id = $2",
                    uuid.UUID(agent.json()["key_id"]),
                    profile_id,
                )
                == 1
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM memory_profile_events "
                    "WHERE profile_id = $1 AND revision_id = $2 "
                    "AND event_type = 'profile_bound_at_key_issuance'",
                    profile_id,
                    uuid.UUID(revision_id),
                )
                == 1
            )

            await client.post(f"/v1/memory-profiles/{profile_id}/disable", json={"reason": "pause"})
            rejected_name = f"rejected-agent-{uuid.uuid4().hex[:10]}"
            before = await owner.fetchval(
                "SELECT count(*) FROM principals WHERE name = $1", rejected_name
            )
            rejected = await client.post(
                "/v1/agents",
                json={"name": rejected_name, "memory_profile_id": str(profile_id)},
            )
            assert rejected.status_code == 404
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM principals WHERE name = $1", rejected_name
                )
                == before
            )
    finally:
        if agent_ids:
            await owner.execute(
                "DELETE FROM api_keys WHERE principal_id = ANY($1::uuid[])", agent_ids
            )
            await owner.execute("DELETE FROM principals WHERE id = ANY($1::uuid[])", agent_ids)
        if profile_id is not None:
            await owner.execute("DELETE FROM memory_profiles WHERE id = $1", profile_id)
        await owner.close()

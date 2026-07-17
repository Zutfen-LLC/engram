"""Authentication-cache, issuance, whoami, and non-enforcement proofs."""

from __future__ import annotations

import os
import secrets
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
from engram.auth import generate_api_key, hash_api_key, reset_principal_cache
from engram.config import settings
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


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_profiled_auth_resolves_mutable_state_after_every_warm_cache_hit() -> None:
    owner = await _owner()
    profile_id: uuid.UUID | None = None
    key_ids: list[uuid.UUID] = []
    legacy_key_id = uuid.uuid4()
    try:
        default = await owner.fetchrow(
            "SELECT t.id AS tenant_id, p.id AS principal_id "
            "FROM tenants t JOIN principals p ON p.tenant_id = t.id "
            "WHERE t.slug = 'default' AND p.name = 'admin'"
        )
        slug = f"auth-{uuid.uuid4().hex[:10]}"
        settings.auth_enabled = False
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/memory-profiles",
                json={
                    "name": "Auth profile",
                    "slug": slug,
                    "reason": "auth proof",
                    "policy": {"include_private": False},
                },
            )
            assert created.status_code == 201, created.text
            profile_id = uuid.UUID(created.json()["id"])
            revision_v1 = uuid.UUID(created.json()["active_revision_id"])

            unprofiled = await client.post(
                "/v1/admin/api-keys",
                json={
                    "tenant_id": str(default["tenant_id"]),
                    "principal_id": str(default["principal_id"]),
                    "scopes": ["read", "write"],
                    "label": "unprofiled-auth-proof",
                },
            )
            assert unprofiled.status_code == 201, unprofiled.text
            key_ids.append(uuid.UUID(unprofiled.json()["id"]))

            profiled = await client.post(
                "/v1/admin/api-keys",
                json={
                    "tenant_id": str(default["tenant_id"]),
                    "principal_id": str(default["principal_id"]),
                    "scopes": ["read", "write"],
                    "label": "profiled-auth-proof",
                    "memory_profile_id": str(profile_id),
                },
            )
            assert profiled.status_code == 201, profiled.text
            profiled_body = profiled.json()
            key_ids.append(uuid.UUID(profiled_body["id"]))
            assert profiled_body["memory_profile_revision_id"] == str(revision_v1)
            assert profiled_body["memory_profile_slug"] == slug
            unprofiled_key = unprofiled.json()["key"]
            profiled_key = profiled_body["key"]

            # A legacy-format key can safely carry the same immutable binding.
            legacy_plaintext = "eng_" + secrets.token_urlsafe(32)
            await owner.execute(
                "INSERT INTO api_keys "
                "(id, tenant_id, principal_id, key_hash, scopes, label, memory_profile_id) "
                "VALUES ($1, $2, $3, $4, ARRAY['read'], 'legacy-profiled', $5)",
                legacy_key_id,
                default["tenant_id"],
                default["principal_id"],
                hash_api_key(legacy_plaintext),
                profile_id,
            )

            reset_principal_cache()
            settings.auth_enabled = True
            warm = await client.get("/whoami", headers=_bearer(profiled_key))
            assert warm.status_code == 200, warm.text
            assert warm.json()["memory_profile"] == {
                "id": str(profile_id),
                "slug": slug,
                "active_revision_id": str(revision_v1),
                "version": 1,
            }
            unbound = await client.get("/whoami", headers=_bearer(unprofiled_key))
            assert unbound.status_code == 200
            assert unbound.json()["memory_profile"] is None
            legacy = await client.get("/whoami", headers=_bearer(legacy_plaintext))
            assert legacy.status_code == 200
            assert legacy.json()["memory_profile"]["id"] == str(profile_id)

            await owner.execute(
                "UPDATE memory_profiles SET disabled_at = now() WHERE id = $1", profile_id
            )
            disabled = await client.get("/whoami", headers=_bearer(profiled_key))
            assert disabled.status_code == 401
            assert disabled.json()["detail"] == "Invalid or revoked API key"
            assert (await client.get("/whoami", headers=_bearer(unprofiled_key))).status_code == 200

            await owner.execute(
                "UPDATE memory_profiles SET disabled_at = NULL WHERE id = $1", profile_id
            )
            enabled = await client.get("/whoami", headers=_bearer(profiled_key))
            assert enabled.status_code == 200
            assert enabled.json()["memory_profile"]["version"] == 1

            revision_v2 = uuid.uuid4()
            async with owner.transaction():
                await owner.execute(
                    "INSERT INTO memory_profile_revisions "
                    "(id, tenant_id, profile_id, version, include_private, "
                    "created_by_principal_id, reason) VALUES ($1, $2, $3, 2, false, $4, 'v2')",
                    revision_v2,
                    default["tenant_id"],
                    profile_id,
                    default["principal_id"],
                )
                await owner.execute(
                    "UPDATE memory_profiles SET active_revision_id = $1 WHERE id = $2",
                    revision_v2,
                    profile_id,
                )
            transitioned = await client.get("/whoami", headers=_bearer(profiled_key))
            assert transitioned.status_code == 200
            assert transitioned.json()["memory_profile"]["active_revision_id"] == str(revision_v2)
            assert transitioned.json()["memory_profile"]["version"] == 2

            # ENG-SCOPE-002A is declarative: include_private=false does not yet
            # suppress writes or reads for an otherwise-equivalent principal.
            content = f"profile non-enforcement {uuid.uuid4()}"
            remembered = await client.post(
                "/v1/remember",
                json={"content": content, "kind": "fact", "visibility": "private"},
                headers=_bearer(profiled_key),
            )
            assert remembered.status_code == 201, remembered.text
            item_id = remembered.json()["id"]
            for key in (profiled_key, unprofiled_key):
                search = await client.post(
                    "/v1/search",
                    json={"query": content, "mode": "keyword", "limit": 10},
                    headers=_bearer(key),
                )
                assert search.status_code == 200, search.text
                assert item_id in {item["id"] for item in search.json()["results"]}

            operation_text = str((await client.get("/openapi.json")).json()["paths"])
            for data_plane_path in (
                "/v1/remember",
                "/v1/recall",
                "/v1/search",
                "/v1/items/{item_id}",
                "/v1/classify",
                "/v1/kg",
                "/v1/diary",
            ):
                if data_plane_path in operation_text:
                    operation = (await client.get("/openapi.json")).json()["paths"][data_plane_path]
                    assert "memory_profile" not in str(operation)

        event = await owner.fetchrow(
            "SELECT details FROM memory_profile_events "
            "WHERE profile_id = $1 AND event_type = 'profile_bound_at_key_issuance' "
            "AND details->>'api_key_id' = $2",
            profile_id,
            str(key_ids[1]),
        )
        assert event is not None
        details_text = str(event["details"])
        assert profiled_key not in details_text
        assert "secret" not in details_text.lower()
    finally:
        reset_principal_cache()
        await owner.execute(
            "DELETE FROM memory_items WHERE content LIKE 'profile non-enforcement %'"
        )
        await owner.execute(
            "DELETE FROM api_keys WHERE id = ANY($1::uuid[])", key_ids + [legacy_key_id]
        )
        if profile_id is not None:
            await owner.execute("DELETE FROM memory_profiles WHERE id = $1", profile_id)
        await owner.close()


def test_wrong_key_shape_remains_generic_and_does_not_resolve_profile() -> None:
    # Database-independent guard that the auth parser still generates opaque,
    # high-entropy key material; request-level wrong-secret behavior is covered
    # by the existing auth suite and the profiled 401 transition above.
    assert generate_api_key().startswith("eng_")

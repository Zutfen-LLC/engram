"""Real-PostgreSQL profile read boundary across caller-facing surfaces."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
from engram.auth import reset_principal_cache
from engram.config import settings
from engram.migrations import normalize_asyncpg_url


def _dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get(
        "ENGRAM_DATABASE_URL"
    )


async def _owner():
    import asyncpg

    if not _dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")
    try:
        return await asyncpg.connect(normalize_asyncpg_url(_dsn()))  # type: ignore[arg-type]
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema")


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _profile_body(
    slug: str,
    *,
    private: bool,
    tenant: bool,
    public: bool,
    workspace_ids: list[uuid.UUID],
) -> dict[str, object]:
    return {
        "name": slug,
        "slug": slug,
        "reason": "profile read matrix",
        "policy": {
            "include_private": private,
            "include_tenant": tenant,
            "include_public": public,
            "workspace_grants": [
                {"workspace_id": str(workspace_id), "can_read": True, "can_write": False}
                for workspace_id in workspace_ids
            ],
        },
    }


async def _create_key(
    client: AsyncClient,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    label: str,
    profile_id: uuid.UUID | None,
) -> tuple[uuid.UUID, str]:
    payload: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "principal_id": str(principal_id),
        "scopes": ["admin"],
        "label": label,
    }
    if profile_id is not None:
        payload["memory_profile_id"] = str(profile_id)
    response = await client.post("/v1/admin/api-keys", json=payload)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"]), response.json()["key"]


async def test_profile_read_matrix_revision_audit_and_write_non_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _owner()
    token = uuid.uuid4().hex[:10]
    created_profiles: list[uuid.UUID] = []
    created_keys: list[uuid.UUID] = []
    item_ids: list[uuid.UUID] = []
    ingest_ids: list[uuid.UUID] = []
    recall_log_ids: list[uuid.UUID] = []
    usage_started_at: datetime | None = None
    original_auto_promote: bool | None = None
    workspace_ids = [uuid.uuid4(), uuid.uuid4()]
    other_principal_id = uuid.uuid4()
    settings.auth_enabled = False
    original_embedding_provider = settings.embedding_provider
    original_usage_telemetry = settings.usage_telemetry_enabled
    try:
        default = await owner.fetchrow(
            "SELECT t.id AS tenant_id, p.id AS principal_id "
            "FROM tenants t JOIN principals p ON p.tenant_id = t.id "
            "WHERE t.slug = 'default' AND p.name = 'admin'"
        )
        tenant_id = default["tenant_id"]
        principal_id = default["principal_id"]
        usage_started_at = await owner.fetchval("SELECT clock_timestamp()")
        original_auto_promote = await owner.fetchval(
            "SELECT auto_promote_enabled FROM tenant_config WHERE tenant_id = $1", tenant_id
        )
        await owner.execute(
            "INSERT INTO principals (id, tenant_id, name, type) VALUES ($1, $2, $3, 'agent')",
            other_principal_id,
            tenant_id,
            f"profile-other-{token}",
        )
        for index, workspace_id in enumerate(workspace_ids):
            await owner.execute(
                "INSERT INTO workspaces (id, tenant_id, name, slug) VALUES ($1, $2, $3, $4)",
                workspace_id,
                tenant_id,
                f"Profile {index} {token}",
                f"profile-{index}-{token}",
            )
            await owner.execute(
                "INSERT INTO workspace_members (workspace_id, principal_id, role) "
                "VALUES ($1, $2, 'member')",
                workspace_id,
                principal_id,
            )
        await owner.execute(
            "UPDATE tenant_config SET auto_promote_enabled = false WHERE tenant_id = $1",
            tenant_id,
        )

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            all_a_response = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"all-a-{token}",
                    private=True,
                    tenant=True,
                    public=True,
                    workspace_ids=[workspace_ids[0]],
                ),
            )
            empty_response = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"empty-{token}",
                    private=False,
                    tenant=False,
                    public=False,
                    workspace_ids=[],
                ),
            )
            workspace_response = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"workspace-a-{token}",
                    private=False,
                    tenant=False,
                    public=False,
                    workspace_ids=[workspace_ids[0]],
                ),
            )
            private_response = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"private-{token}",
                    private=True,
                    tenant=False,
                    public=False,
                    workspace_ids=[],
                ),
            )
            tenant_response = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"tenant-{token}",
                    private=False,
                    tenant=True,
                    public=False,
                    workspace_ids=[],
                ),
            )
            public_response = await client.post(
                "/v1/memory-profiles",
                json=_profile_body(
                    f"public-{token}",
                    private=False,
                    tenant=False,
                    public=True,
                    workspace_ids=[],
                ),
            )
            for response in (
                all_a_response,
                empty_response,
                workspace_response,
                private_response,
                tenant_response,
                public_response,
            ):
                assert response.status_code == 201, response.text
            all_a = all_a_response.json()
            all_a_id = uuid.UUID(all_a["id"])
            empty_id = uuid.UUID(empty_response.json()["id"])
            workspace_id = uuid.UUID(workspace_response.json()["id"])
            private_id = uuid.UUID(private_response.json()["id"])
            tenant_id_profile = uuid.UUID(tenant_response.json()["id"])
            public_id = uuid.UUID(public_response.json()["id"])
            created_profiles.extend(
                (all_a_id, empty_id, workspace_id, private_id, tenant_id_profile, public_id)
            )

            for label, profile_id in (
                (f"unprofiled-{token}", None),
                (f"all-a-{token}", all_a_id),
                (f"empty-{token}", empty_id),
                (f"workspace-{token}", workspace_id),
                (f"private-{token}", private_id),
                (f"tenant-{token}", tenant_id_profile),
                (f"public-{token}", public_id),
            ):
                key_id, key = await _create_key(
                    client,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    label=label,
                    profile_id=profile_id,
                )
                created_keys.append(key_id)
                if profile_id is None:
                    unprofiled_key = key
                elif profile_id == all_a_id:
                    all_a_key = key
                elif profile_id == empty_id:
                    empty_key = key
                elif profile_id == workspace_id:
                    workspace_key = key
                elif profile_id == private_id:
                    private_key = key
                elif profile_id == tenant_id_profile:
                    tenant_key = key
                else:
                    public_key = key

            other_key_id, other_key = await _create_key(
                client,
                tenant_id=tenant_id,
                principal_id=other_principal_id,
                label=f"other-all-a-{token}",
                profile_id=all_a_id,
            )
            created_keys.append(other_key_id)

            rows = [
                ("private-null", "private", None, principal_id, "active", "fact"),
                ("private-a", "private", workspace_ids[0], principal_id, "active", "fact"),
                ("private-b", "private", workspace_ids[1], principal_id, "active", "fact"),
                ("private-other", "private", None, other_principal_id, "active", "fact"),
                ("tenant-null", "tenant", None, other_principal_id, "active", "fact"),
                ("tenant-a", "tenant", workspace_ids[0], other_principal_id, "active", "doctrine"),
                ("tenant-b", "tenant", workspace_ids[1], other_principal_id, "active", "doctrine"),
                ("public-null", "public", None, other_principal_id, "active", "fact"),
                ("public-a", "public", workspace_ids[0], other_principal_id, "active", "fact"),
                ("public-b", "public", workspace_ids[1], other_principal_id, "active", "fact"),
                (
                    "workspace-a", "workspace", workspace_ids[0], other_principal_id,
                    "active", "fact",
                ),
                (
                    "workspace-b", "workspace", workspace_ids[1], other_principal_id,
                    "active", "fact",
                ),
                (
                    "proposed-a", "workspace", workspace_ids[0], other_principal_id,
                    "proposed", "fact",
                ),
                ("diary", "private", None, principal_id, "active", "diary_entry"),
            ]
            item_names: dict[uuid.UUID, str] = {}
            for name, visibility, item_workspace_id, owner_id, review_status, kind in rows:
                item_id = uuid.uuid4()
                item_ids.append(item_id)
                item_names[item_id] = name
                await owner.execute(
                    "INSERT INTO memory_items "
                    "(id, tenant_id, workspace_id, principal_id, content, content_hash, kind, "
                    "visibility, review_status, wing, room, source_type) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'profile-wing', "
                    "'profile-room', 'manual')",
                    item_id,
                    tenant_id,
                    item_workspace_id,
                    owner_id,
                    f"scopeprofiletoken {token} {name}",
                    f"sha256:{uuid.uuid4().hex}",
                    kind,
                    visibility,
                    review_status,
                )
            embedding_profile = await owner.fetchrow(
                "SELECT id, model, dimensions FROM embedding_profiles WHERE state = 'active'"
            )
            embedding_dim = int(embedding_profile["dimensions"])
            exact_vector = "[" + ",".join(["1"] + ["0"] * (embedding_dim - 1)) + "]"
            lower_vector = (
                "[" + ",".join(["0.8", "0.6"] + ["0"] * (embedding_dim - 2)) + "]"
            )
            for name, vector in (("tenant-a", lower_vector), ("tenant-b", exact_vector)):
                await owner.execute(
                    "INSERT INTO memory_embeddings "
                    "(memory_item_id, tenant_id, profile_id, embedding_model, embedding_dim, "
                    "embedding, embedding_status) VALUES ($1, $2, $3, $4, $5, $6::vector, 'ready')",
                    next(item_id for item_id, item_name in item_names.items() if item_name == name),
                    tenant_id,
                    embedding_profile["id"],
                    embedding_profile["model"],
                    embedding_dim,
                    vector,
                )
            triple_a, triple_b = uuid.uuid4(), uuid.uuid4()
            await owner.execute(
                "INSERT INTO kg_triples "
                "(id, tenant_id, workspace_id, principal_id, subject, predicate, object, "
                "source_item_id, review_status) VALUES "
                "($1, $2, $3, $4, $5, 'relates', 'a', $6, 'active'), "
                "($7, $2, $8, $4, $5, 'relates', 'b', $9, 'active')",
                triple_a,
                tenant_id,
                workspace_ids[0],
                principal_id,
                f"entity-{token}",
                next(item_id for item_id, name in item_names.items() if name == "tenant-a"),
                triple_b,
                workspace_ids[1],
                next(item_id for item_id, name in item_names.items() if name == "tenant-b"),
            )

            reset_principal_cache()
            settings.auth_enabled = True
            settings.embedding_provider = "openai"
            settings.usage_telemetry_enabled = True

            async def fake_embedding(*_args: object, **_kwargs: object) -> list[float]:
                return [1.0] + [0.0] * (embedding_dim - 1)

            from engram import recall as recall_module
            from engram.api.routes import memory as memory_routes

            monkeypatch.setattr(recall_module, "generate_embedding", fake_embedding)
            monkeypatch.setattr(memory_routes, "generate_embedding", fake_embedding)

            async def listed(key: str, **params: object) -> set[str]:
                response = await client.get(
                    "/v1/items",
                    params={"active_only": "false", "limit": 100, **params},
                    headers=_headers(key),
                )
                assert response.status_code == 200, response.text
                return {
                    str(item["content"]).rsplit(" ", 1)[-1]
                    for item in response.json()["items"]
                    if token in item["content"]
                }

            assert await listed(all_a_key) == {
                "private-null", "private-a", "tenant-null", "tenant-a", "public-null",
                "public-a", "workspace-a", "proposed-a", "diary",
            }
            assert await listed(workspace_key) == {"workspace-a", "proposed-a"}
            assert await listed(empty_key) == set()
            assert await listed(private_key) == {"private-null", "diary"}
            assert await listed(tenant_key) == {"tenant-null"}
            assert await listed(public_key) == {"public-null"}
            assert await listed(unprofiled_key) == {
                "private-null", "private-a", "private-b", "tenant-null", "tenant-a",
                "tenant-b", "public-null", "public-a", "public-b", "workspace-a",
                "workspace-b", "proposed-a", "diary",
            }
            # Tenant/public visibility does not acquire a principal-membership
            # requirement merely because the item is workspace-associated.
            # Conversely, the grant never widens workspace/private visibility.
            assert await listed(other_key) == {
                "private-other", "tenant-null", "tenant-a", "public-null", "public-a"
            }
            assert await listed(all_a_key, workspace=f"profile-0-{token}") == {
                "private-a", "tenant-a", "public-a", "workspace-a", "proposed-a",
            }
            assert await listed(all_a_key, workspace=f"profile-1-{token}") == set()
            assert await listed(all_a_key, workspace=f"missing-{token}") == set()
            assert await listed(other_key, workspace=f"profile-0-{token}") == set()

            private_b_id = next(
                item_id for item_id, name in item_names.items() if name == "private-b"
            )
            assert (
                await client.get(f"/v1/items/{private_b_id}", headers=_headers(all_a_key))
            ).status_code == 404
            assert (
                await client.get(f"/v1/items/{private_b_id}", headers=_headers(unprofiled_key))
            ).status_code == 200
            private_a_id = next(
                item_id for item_id, name in item_names.items() if name == "private-a"
            )
            private_a_detail = await client.get(
                f"/v1/items/{private_a_id}", headers=_headers(all_a_key)
            )
            assert private_a_detail.status_code == 200
            assert private_a_detail.json()["item"]["id"] == str(private_a_id)

            search = await client.post(
                "/v1/search",
                json={"query": f"scopeprofiletoken {token}", "mode": "keyword", "limit": 100},
                headers=_headers(all_a_key),
            )
            assert search.status_code == 200, search.text
            assert {
                result["content"].rsplit(" ", 1)[-1] for result in search.json()["results"]
            } == {
                "private-null", "private-a", "tenant-null", "tenant-a", "public-null",
                "public-a", "workspace-a", "diary",
            }

            for mode in ("semantic", "hybrid"):
                semantic_search = await client.post(
                    "/v1/search",
                    json={
                        "query": f"scopeprofiletoken {token}",
                        "mode": mode,
                        "limit": 1 if mode == "semantic" else 100,
                    },
                    headers=_headers(all_a_key),
                )
                assert semantic_search.status_code == 200, semantic_search.text
                semantic_names = {
                    result["content"].rsplit(" ", 1)[-1]
                    for result in semantic_search.json()["results"]
                }
                assert "tenant-a" in semantic_names
                assert "tenant-b" not in semantic_names

            telemetry = await owner.fetchrow(
                "SELECT id, metadata FROM usage_events "
                "WHERE event_type = 'retrieval.request' AND operation = 'semantic_search' "
                "AND principal_id = $1 ORDER BY created_at DESC LIMIT 1",
                principal_id,
            )
            telemetry_metadata = json.loads(telemetry["metadata"])
            assert telemetry_metadata["memory_context_version"] == "memory-context-v1"
            assert telemetry_metadata["memory_profile_id"] == str(all_a_id)
            assert telemetry_metadata["memory_profile_revision_id"] == all_a[
                "active_revision_id"
            ]
            assert telemetry_metadata["memory_profile_version"] == 1
            assert "query" not in telemetry_metadata
            assert "content" not in telemetry_metadata

            recall = await client.post(
                "/v1/recall",
                json={"mode": "startup", "byte_budget": 100000},
                headers=_headers(all_a_key),
            )
            assert recall.status_code == 200, recall.text
            recall_log_ids.append(uuid.UUID(recall.json()["recall_log_id"]))
            recalled = {
                item["content"].rsplit(" ", 1)[-1] for item in recall.json()["items"]
            }
            assert "private-b" not in recalled
            assert "workspace-b" not in recalled
            assert "tenant-a" in recalled

            semantic_recall = await client.post(
                "/v1/recall",
                json={"mode": "semantic", "query": "scope profile", "item_budget": 1},
                headers=_headers(all_a_key),
            )
            assert semantic_recall.status_code == 200, semantic_recall.text
            recall_log_ids.append(uuid.UUID(semantic_recall.json()["recall_log_id"]))
            semantic_recalled = {
                item["content"].rsplit(" ", 1)[-1]
                for item in semantic_recall.json()["items"]
            }
            assert semantic_recalled == {"tenant-a"}

            unprofiled_recall = await client.post(
                "/v1/recall",
                json={"mode": "startup", "byte_budget": 100000},
                headers=_headers(unprofiled_key),
            )
            assert unprofiled_recall.status_code == 200, unprofiled_recall.text
            unprofiled_log_id = uuid.UUID(unprofiled_recall.json()["recall_log_id"])
            recall_log_ids.append(unprofiled_log_id)
            unprofiled_log = await owner.fetchrow(
                "SELECT memory_profile_id, memory_profile_revision_id, memory_context_version "
                "FROM recall_logs WHERE id = $1",
                unprofiled_log_id,
            )
            assert unprofiled_log["memory_profile_id"] is None
            assert unprofiled_log["memory_profile_revision_id"] is None
            assert unprofiled_log["memory_context_version"] == "memory-context-v1"

            queue = await client.get("/v1/review/queue", headers=_headers(all_a_key))
            assert queue.status_code == 200, queue.text
            assert {
                item["content"].rsplit(" ", 1)[-1]
                for item in queue.json()
                if token in item["content"]
            } == {"proposed-a"}
            kg = await client.get(
                "/v1/kg/query",
                params={"entity": f"entity-{token}"},
                headers=_headers(all_a_key),
            )
            assert kg.status_code == 200
            assert [fact["object"] for fact in kg.json()] == ["a"]
            diary = await client.get("/v1/diary/admin", headers=_headers(workspace_key))
            assert diary.status_code == 200
            assert diary.json() == []
            exported = await client.get("/v1/export/cca", headers=_headers(all_a_key))
            assert exported.status_code == 200
            exported_names = {
                entry["text"].rsplit(" ", 1)[-1]
                for entry in exported.json()["entries"]
                if token in entry["text"]
            }
            assert exported_names == {"tenant-a"}
            taxonomy = await client.get("/v1/taxonomy", headers=_headers(all_a_key))
            assert taxonomy.status_code == 200
            profile_wing = next(
                wing for wing in taxonomy.json()["wings"] if wing["name"] == "profile-wing"
            )
            assert profile_wing["item_count"] == 8

            for profiled_log_id in recall_log_ids[:2]:
                log = await owner.fetchrow(
                    "SELECT memory_profile_id, memory_profile_revision_id, "
                    "memory_context_version FROM recall_logs WHERE id = $1",
                    profiled_log_id,
                )
                assert log["memory_profile_id"] == all_a_id
                assert str(log["memory_profile_revision_id"]) == all_a[
                    "active_revision_id"
                ]
                assert log["memory_context_version"] == "memory-context-v1"

            revision = await client.post(
                f"/v1/memory-profiles/{all_a_id}/revisions",
                json={
                    "expected_active_revision_id": all_a["active_revision_id"],
                    "reason": "empty next revision",
                    "policy": {
                        "include_private": False,
                        "include_tenant": False,
                        "include_public": False,
                    },
                },
                headers=_headers(unprofiled_key),
            )
            assert revision.status_code == 200, revision.text
            reset_principal_cache()
            assert await listed(all_a_key) == set()

            remembered = await client.post(
                "/v1/remember",
                json={
                    "content": f"scopeprofiletoken {token} write-still-allowed",
                    "kind": "fact",
                    "visibility": "private",
                },
                headers=_headers(empty_key),
            )
            assert remembered.status_code == 201, remembered.text
            remembered_id = uuid.UUID(remembered.json()["id"])
            item_ids.append(remembered_id)
            ingest_ids.append(uuid.UUID(remembered.json()["ingest_id"]))
            assert "write-still-allowed" not in await listed(empty_key)
            assert "write-still-allowed" in await listed(unprofiled_key)
    finally:
        settings.auth_enabled = False
        settings.embedding_provider = original_embedding_provider
        settings.usage_telemetry_enabled = original_usage_telemetry
        reset_principal_cache()
        if usage_started_at is not None:
            await owner.execute(
                "DELETE FROM usage_events WHERE principal_id = $1 AND created_at >= $2",
                principal_id,
                usage_started_at,
            )
        if recall_log_ids:
            await owner.execute(
                "DELETE FROM jobs WHERE payload->>'recall_log_id' = ANY($1::text[])",
                [str(value) for value in recall_log_ids],
            )
            await owner.execute(
                "DELETE FROM recall_logs WHERE id = ANY($1::uuid[])", recall_log_ids
            )
        if item_ids:
            await owner.execute(
                "DELETE FROM jobs WHERE payload->>'memory_item_id' = ANY($1::text[])",
                [str(value) for value in item_ids],
            )
            await owner.execute(
                "DELETE FROM item_events WHERE item_id = ANY($1::uuid[])", item_ids
            )
            await owner.execute(
                "DELETE FROM kg_triples WHERE source_item_id = ANY($1::uuid[])", item_ids
            )
            await owner.execute(
                "DELETE FROM memory_embeddings WHERE memory_item_id = ANY($1::uuid[])", item_ids
            )
            await owner.execute("DELETE FROM memory_items WHERE id = ANY($1::uuid[])", item_ids)
        if ingest_ids:
            await owner.execute(
                "DELETE FROM candidate_ingests WHERE id = ANY($1::uuid[])", ingest_ids
            )
        if created_keys:
            await owner.execute("DELETE FROM api_keys WHERE id = ANY($1::uuid[])", created_keys)
        if created_profiles:
            await owner.execute(
                "DELETE FROM memory_profiles WHERE id = ANY($1::uuid[])", created_profiles
            )
        await owner.execute(
            "DELETE FROM workspace_members WHERE workspace_id = ANY($1::uuid[])", workspace_ids
        )
        await owner.execute("DELETE FROM workspaces WHERE id = ANY($1::uuid[])", workspace_ids)
        await owner.execute("DELETE FROM principals WHERE id = $1", other_principal_id)
        if original_auto_promote is not None:
            await owner.execute(
                "UPDATE tenant_config SET auto_promote_enabled = $1 WHERE tenant_id = "
                "(SELECT id FROM tenants WHERE slug = 'default')",
                original_auto_promote,
            )
        await owner.close()

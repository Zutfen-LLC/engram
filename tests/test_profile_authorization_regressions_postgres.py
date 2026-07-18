"""Real-PostgreSQL regressions for profile-bound mutation authority."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from engram.api.app import create_app
from engram.auth import reset_principal_cache
from engram.config import settings
from engram.db import owner_session_factory
from engram.memory_access import apply_write_eligibility
from engram.memory_context import memory_context_from_ingest
from engram.migrations import normalize_asyncpg_url
from engram.models import CandidateIngest, MemoryItem


def _dsn() -> str | None:
    return os.environ.get("ENGRAM_OWNER_DATABASE_URL") or os.environ.get("ENGRAM_DATABASE_URL")


async def _owner() -> Any:
    import asyncpg

    if not _dsn():
        pytest.skip("requires owner and app PostgreSQL URLs")
    try:
        connection = await asyncpg.connect(normalize_asyncpg_url(_dsn()))  # type: ignore[arg-type]
        exists = await connection.fetchval("SELECT to_regclass('candidate_ingest_executions')")
        if exists is None:
            await connection.close()
            pytest.skip("requires migration 025")
        return connection
    except Exception:
        pytest.skip("requires a live PostgreSQL with the current schema")


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _profile(slug: str, **policy: bool) -> dict[str, object]:
    return {
        "name": slug,
        "slug": slug,
        "reason": "authorization regression",
        "policy": {
            "include_private": policy.get("include_private", True),
            "include_tenant": policy.get("include_tenant", False),
            "include_public": policy.get("include_public", False),
            "allow_tenant_write": policy.get("allow_tenant_write", False),
            "allow_public_write": policy.get("allow_public_write", False),
        },
    }


async def test_profile_bound_mutation_regressions() -> None:
    owner = await _owner()
    token = uuid.uuid4().hex[:10]
    other_principal = uuid.uuid4()
    profile_ids: list[uuid.UUID] = []
    key_ids: list[uuid.UUID] = []
    item_ids: list[uuid.UUID] = []
    ingest_ids: list[uuid.UUID] = []
    classification_run_ids: list[uuid.UUID] = []
    original_auth = settings.auth_enabled
    original_provider = settings.embedding_provider
    original_promotion: tuple[bool, float, int] | None = None
    tenant_id: uuid.UUID | None = None
    try:
        default = await owner.fetchrow(
            "SELECT t.id AS tenant_id, p.id AS principal_id FROM tenants t "
            "JOIN principals p ON p.tenant_id=t.id "
            "WHERE t.slug='default' AND p.name='admin'"
        )
        tenant_id = default["tenant_id"]
        principal_id = default["principal_id"]
        promotion_row = await owner.fetchrow(
            "SELECT auto_promote_enabled, auto_promote_confidence_threshold, "
            "auto_promote_min_age_hours FROM tenant_config WHERE tenant_id=$1",
            tenant_id,
        )
        original_promotion = (
            promotion_row["auto_promote_enabled"],
            promotion_row["auto_promote_confidence_threshold"],
            promotion_row["auto_promote_min_age_hours"],
        )
        await owner.execute(
            "INSERT INTO principals (id,tenant_id,name,type) VALUES ($1,$2,$3,'agent')",
            other_principal,
            tenant_id,
            f"profile-regression-other-{token}",
        )
        settings.auth_enabled = False
        settings.embedding_provider = "none"
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            definitions = {
                "broad": _profile(
                    f"broad-{token}",
                    include_private=True,
                    include_tenant=True,
                    include_public=True,
                    allow_tenant_write=True,
                    allow_public_write=True,
                ),
                "private": _profile(f"private-{token}", include_private=True),
                "tenant_write": _profile(
                    f"tenant-write-{token}", include_private=True, allow_tenant_write=True
                ),
                "public_write": _profile(
                    f"public-write-{token}", include_private=True, allow_public_write=True
                ),
            }
            profiles: dict[str, dict[str, object]] = {}
            keys: dict[str, str] = {}
            for name, body in definitions.items():
                response = await client.post("/v1/memory-profiles", json=body)
                assert response.status_code == 201, response.text
                profiles[name] = response.json()
                profile_id = uuid.UUID(response.json()["id"])
                profile_ids.append(profile_id)
                key_response = await client.post(
                    "/v1/admin/api-keys",
                    json={
                        "tenant_id": str(tenant_id),
                        "principal_id": str(principal_id),
                        "scopes": ["admin"],
                        "label": f"{name}-{token}",
                        "memory_profile_id": str(profile_id),
                    },
                )
                assert key_response.status_code == 201, key_response.text
                key_ids.append(uuid.UUID(key_response.json()["id"]))
                keys[name] = key_response.json()["key"]

            settings.auth_enabled = True
            reset_principal_cache()

            # A profile-bound admin must not even scan another principal's private proposal.
            hidden_item = uuid.uuid4()
            item_ids.append(hidden_item)
            await owner.execute(
                "INSERT INTO memory_items "
                "(id,tenant_id,principal_id,content,content_hash,kind,visibility,review_status,"
                "memory_confidence,source_trust,importance,source_type) "
                "VALUES ($1,$2,$3,$4,$5,'fact','private','proposed',.95,.9,.5,'manual')",
                hidden_item,
                tenant_id,
                other_principal,
                f"hidden promotion {token}",
                f"sha256:{uuid.uuid4().hex}",
            )
            await owner.execute(
                "UPDATE tenant_config SET auto_promote_enabled=false WHERE tenant_id=$1",
                tenant_id,
            )
            # The promotion scan runs against the shared default tenant. Other
            # suites can leave admin-owned proposed rows behind, which a broad
            # admin profile would legitimately scan (private + owned by admin).
            # Remove only the caller-principal's stale proposed rows so this
            # assertion measures exactly the hidden other-principal item, which
            # the profile-bound admin must never see. Other principals' rows are
            # untouched.
            await owner.execute(
                "DELETE FROM memory_items "
                "WHERE tenant_id=$1 AND principal_id=$2 "
                "AND review_status='proposed' AND valid_to IS NULL",
                tenant_id,
                principal_id,
            )
            preview = await client.post(
                "/v1/admin/promote?dry_run=true", headers=_headers(keys["broad"])
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["scanned"] == 0
            assert (
                await owner.fetchval(
                    "SELECT review_status FROM memory_items WHERE id=$1", hidden_item
                )
                == "proposed"
            )

            # Write-only target visibility may make the row unreadable after a successful PATCH.
            for profile_name, visibility in (
                ("tenant_write", "tenant"),
                ("public_write", "public"),
            ):
                remembered = await client.post(
                    "/v1/remember",
                    json={"content": f"patch {visibility} {token}", "visibility": "private"},
                    headers=_headers(keys[profile_name]),
                )
                assert remembered.status_code == 201, remembered.text
                item_id = uuid.UUID(remembered.json()["id"])
                item_ids.append(item_id)
                ingest_ids.append(uuid.UUID(remembered.json()["ingest_id"]))
                patched = await client.patch(
                    f"/v1/items/{item_id}",
                    json={"visibility": visibility, "reason": "write-only transition"},
                    headers=_headers(keys[profile_name]),
                )
                assert patched.status_code == 200, patched.text
                assert patched.json()["item"]["visibility"] == visibility
                assert (
                    await owner.fetchval("SELECT visibility FROM memory_items WHERE id=$1", item_id)
                    == visibility
                )

            # Classify under broad A, then consume under private-only B.
            content = f"cross-profile execution {token}"
            classified = await client.post(
                "/v1/classify",
                json={"content": content, "visibility": "private"},
                headers=_headers(keys["broad"]),
            )
            assert classified.status_code == 200, classified.text
            ingest_id = uuid.UUID(classified.json()["ingest_id"])
            ingest_ids.append(ingest_id)
            classification_run_ids.append(uuid.UUID(classified.json()["classification_run_id"]))
            remembered = await client.post(
                "/v1/remember",
                json={
                    "content": content,
                    "visibility": "private",
                    "classification_run_id": classified.json()["classification_run_id"],
                },
                headers=_headers(keys["private"]),
            )
            assert remembered.status_code == 201, remembered.text
            item_ids.append(uuid.UUID(remembered.json()["id"]))
            origin, execution = await owner.fetchrow(
                "SELECT ci.memory_profile_id AS origin, ce.memory_profile_id AS execution "
                "FROM candidate_ingests ci JOIN candidate_ingest_executions ce "
                "ON ce.ingest_id=ci.id WHERE ci.id=$1",
                ingest_id,
            )
            assert origin == uuid.UUID(str(profiles["broad"]["id"]))
            assert execution == uuid.UUID(str(profiles["private"]["id"]))
            async with owner_session_factory() as session:
                ingest = await session.scalar(
                    select(CandidateIngest).where(CandidateIngest.id == ingest_id)
                )
                assert ingest is not None
                worker_context = await memory_context_from_ingest(session, ingest)
            assert worker_context is not None
            assert worker_context.memory_profile_id == uuid.UUID(str(profiles["private"]["id"]))
            assert worker_context.include_tenant is False
            assert worker_context.include_public is False
            assert worker_context.allow_tenant_write is False
            assert worker_context.allow_public_write is False

            tenant_target = uuid.uuid4()
            item_ids.append(tenant_target)
            await owner.execute(
                "INSERT INTO memory_items "
                "(id,tenant_id,principal_id,content,content_hash,kind,visibility,review_status,"
                "memory_confidence,source_trust,importance,source_type) "
                "VALUES ($1,$2,$3,$4,$5,'fact','tenant','active',.9,.9,.5,'manual')",
                tenant_target,
                tenant_id,
                other_principal,
                f"worker tenant target {token}",
                f"sha256:{uuid.uuid4().hex}",
            )
            async with owner_session_factory() as session:
                eligible_target = await session.scalar(
                    apply_write_eligibility(
                        select(MemoryItem).where(MemoryItem.id == tenant_target),
                        worker_context,
                    )
                )
            assert eligible_target is None
    finally:
        settings.auth_enabled = original_auth
        settings.embedding_provider = original_provider
        reset_principal_cache()
        if classification_run_ids:
            await owner.execute(
                "DELETE FROM classification_runs WHERE id=ANY($1::uuid[])",
                classification_run_ids,
            )
        if item_ids:
            await owner.execute("DELETE FROM memory_items WHERE id=ANY($1::uuid[])", item_ids)
        if ingest_ids:
            await owner.execute(
                "DELETE FROM jobs WHERE payload->>'ingest_id'=ANY($1::text[])",
                [str(value) for value in ingest_ids],
            )
            await owner.execute(
                "DELETE FROM usage_events WHERE ingest_id=ANY($1::uuid[])", ingest_ids
            )
            await owner.execute(
                "DELETE FROM candidate_ingests WHERE id=ANY($1::uuid[])", ingest_ids
            )
        if key_ids:
            await owner.execute("DELETE FROM api_keys WHERE id=ANY($1::uuid[])", key_ids)
        if profile_ids:
            await owner.execute("DELETE FROM memory_profiles WHERE id=ANY($1::uuid[])", profile_ids)
        await owner.execute("DELETE FROM principals WHERE id=$1", other_principal)
        if original_promotion is not None and tenant_id is not None:
            await owner.execute(
                "UPDATE tenant_config SET auto_promote_enabled=$1, "
                "auto_promote_confidence_threshold=$2, auto_promote_min_age_hours=$3 "
                "WHERE tenant_id=$4",
                original_promotion[0],
                original_promotion[1],
                original_promotion[2],
                tenant_id,
            )
        await owner.close()

"""Real-PostgreSQL durability regressions for candidate execution-context pinning.

These tests enforce the ENG-SCOPE-002C invariant: the first successful
``/v1/remember`` execution durably pins exactly one execution-context row, a
failed attempt does not consume the ingest, and a later replay under a
different memory context receives a deterministic 409.

They run against the Compose real-PostgreSQL stack (see ``make compose-ci``)
and are skipped without it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from engram.api.app import create_app
from engram.auth import reset_principal_cache
from engram.config import settings
from engram.migrations import normalize_asyncpg_url


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
        "reason": "execution-context durability",
        "policy": {
            "include_private": policy.get("include_private", True),
            "include_tenant": policy.get("include_tenant", False),
            "include_public": policy.get("include_public", False),
            "allow_tenant_write": policy.get("allow_tenant_write", False),
            "allow_public_write": policy.get("allow_public_write", False),
        },
    }


async def _bootstrap(client: AsyncClient, owner: Any, token: str) -> dict[str, Any]:
    """Create two profile-bound admin keys (broad/private) in the default tenant."""
    default = await owner.fetchrow(
        "SELECT t.id AS tenant_id, p.id AS principal_id FROM tenants t "
        "JOIN principals p ON p.tenant_id=t.id "
        "WHERE t.slug='default' AND p.name='admin'"
    )
    tenant_id = default["tenant_id"]
    principal_id = default["principal_id"]
    out: dict[str, Any] = {"tenant_id": tenant_id, "principal_id": principal_id}
    for name, body in {
        "broad": _profile(
            f"broad-{token}",
            include_private=True,
            include_tenant=True,
            include_public=True,
            allow_tenant_write=True,
            allow_public_write=True,
        ),
        "private": _profile(f"private-{token}", include_private=True),
    }.items():
        response = await client.post("/v1/memory-profiles", json=body)
        assert response.status_code == 201, response.text
        profile = response.json()
        out[f"{name}_profile_id"] = uuid.UUID(str(profile["id"]))
        key_response = await client.post(
            "/v1/admin/api-keys",
            json={
                "tenant_id": str(tenant_id),
                "principal_id": str(principal_id),
                "scopes": ["admin"],
                "label": f"{name}-{token}",
                "memory_profile_id": str(profile["id"]),
            },
        )
        assert key_response.status_code == 201, key_response.text
        out[f"{name}_key_id"] = uuid.UUID(str(key_response.json()["id"]))
        out[f"{name}_key"] = key_response.json()["key"]
    return out


def _execution_row(owner: Any, ingest_id: uuid.UUID) -> Any:
    return owner.fetchrow(
        "SELECT api_key_id, memory_profile_id, memory_profile_revision_id, "
        "memory_context_version FROM candidate_ingest_executions WHERE ingest_id=$1",
        ingest_id,
    )


async def _cleanup_ingest(owner: Any, ingest_id: uuid.UUID) -> None:
    """Delete a candidate ingest and its dependent provenance in FK order.

    classification_runs references candidate_ingests with ON DELETE RESTRICT
    (it is evidence, not provenance), so the run must be removed first; then
    usage_events/jobs (RESTRICT on ingest), then the ingest itself (which
    cascades candidate_ingest_executions).
    """
    await owner.execute(
        "DELETE FROM classification_runs WHERE ingest_id=$1", ingest_id
    )
    await owner.execute("DELETE FROM jobs WHERE payload->>'ingest_id'=$1", str(ingest_id))
    await owner.execute("DELETE FROM usage_events WHERE ingest_id=$1", ingest_id)
    await owner.execute("DELETE FROM candidate_ingests WHERE id=$1", ingest_id)


async def test_ordinary_dedup_pins_durable_execution_context() -> None:
    """A successful ordinary dedup (no receipt) must durably pin profile A,
    and a later reuse under profile B must receive 409 without altering the row."""
    owner = await _owner()
    token = uuid.uuid4().hex[:10]
    original_auth = settings.auth_enabled
    original_provider = settings.embedding_provider
    ingest_id: uuid.UUID | None = None
    item_ids: list[uuid.UUID] = []
    key_ids: list[uuid.UUID] = []
    profile_ids: list[uuid.UUID] = []
    try:
        settings.auth_enabled = False
        settings.embedding_provider = "none"
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ctx = await _bootstrap(client, owner, token)
            tenant_id = ctx["tenant_id"]

            settings.auth_enabled = True
            reset_principal_cache()

            content = f"ordinary dedup durability {token}"
            # First write creates the item under profile A (broad).
            first = await client.post(
                "/v1/remember",
                json={"content": content},
                headers=_headers(ctx["broad_key"]),
            )
            assert first.status_code == 201, first.text
            item_ids.append(uuid.UUID(first.json()["id"]))
            ingest_id = uuid.UUID(first.json()["ingest_id"])

            # Second write dedups against the same content and reuses the ingest.
            # No classification receipt -> ordinary dedup path.
            dedup = await client.post(
                "/v1/remember",
                json={"content": content, "ingest_id": str(ingest_id)},
                headers=_headers(ctx["broad_key"]),
            )
            assert dedup.status_code == 201, dedup.text
            assert dedup.json()["status"] == "deduped"
            assert dedup.json()["ingest_id"] == str(ingest_id)

            row = await _execution_row(owner, ingest_id)
            assert row is not None, "execution-context pin was not made durable"
            assert row["memory_profile_id"] == ctx["broad_profile_id"]

            # Reuse under profile B (private) must be rejected with a bounded 409.
            replay = await client.post(
                "/v1/remember",
                json={"content": content, "ingest_id": str(ingest_id)},
                headers=_headers(ctx["private_key"]),
            )
            assert replay.status_code == 409, replay.text

            # The durable row remains profile A, unchanged by the rejected replay.
            row_after = await _execution_row(owner, ingest_id)
            assert row_after is not None
            assert row_after["memory_profile_id"] == ctx["broad_profile_id"]

            # Exactly one execution row and no extra memory item for this content.
            exec_count = await owner.fetchval(
                "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id=$1",
                ingest_id,
            )
            assert exec_count == 1
            item_count = await owner.fetchval(
                "SELECT count(*) FROM memory_items WHERE tenant_id=$1 AND content=$2 "
                "AND valid_to IS NULL",
                tenant_id,
                content,
            )
            assert item_count == 1

            key_ids = [ctx["broad_key_id"], ctx["private_key_id"]]
            profile_ids = [ctx["broad_profile_id"], ctx["private_profile_id"]]
    finally:
        settings.auth_enabled = original_auth
        settings.embedding_provider = original_provider
        reset_principal_cache()
        if ingest_id is not None:
            await _cleanup_ingest(owner, ingest_id)
        if item_ids:
            await owner.execute("DELETE FROM memory_items WHERE id=ANY($1::uuid[])", item_ids)
        if key_ids:
            await owner.execute("DELETE FROM api_keys WHERE id=ANY($1::uuid[])", key_ids)
        if profile_ids:
            await owner.execute(
                "DELETE FROM memory_profiles WHERE id=ANY($1::uuid[])", profile_ids
            )
        await owner.close()


async def test_failed_attempt_does_not_consume_ingest() -> None:
    """A request that pins the execution context but then fails (before a
    successful terminal result) must leave no durable pin; a corrected request
    under a different context then becomes the durable first execution."""
    owner = await _owner()
    token = uuid.uuid4().hex[:10]
    original_auth = settings.auth_enabled
    original_provider = settings.embedding_provider
    ingest_id: uuid.UUID | None = None
    item_ids: list[uuid.UUID] = []
    key_ids: list[uuid.UUID] = []
    profile_ids: list[uuid.UUID] = []
    try:
        settings.auth_enabled = False
        settings.embedding_provider = "none"
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ctx = await _bootstrap(client, owner, token)

            settings.auth_enabled = True
            reset_principal_cache()

            content = f"failed attempt durability {token}"
            # Classify under broad to obtain a receipt with a server-issued ingest.
            classified = await client.post(
                "/v1/classify",
                json={"content": content},
                headers=_headers(ctx["broad_key"]),
            )
            assert classified.status_code == 200, classified.text
            ingest_id = uuid.UUID(classified.json()["ingest_id"])
            run_id = classified.json()["classification_run_id"]

            # Bind the receipt to a memory item under the broad key.
            bound = await client.post(
                "/v1/remember",
                json={"content": content, "classification_run_id": run_id},
                headers=_headers(ctx["broad_key"]),
            )
            assert bound.status_code == 201, bound.text
            item_ids.append(uuid.UUID(bound.json()["id"]))
            assert await _execution_row(owner, ingest_id) is not None

            # A genuine post-pin failure: replay the already-bound receipt but
            # request an explicit kind that differs from the classifier's
            # suggestion. The kind mismatch is a 422 raised AFTER pinning, so
            # this exercises the failed-terminal path without a test backdoor.
            suggested_kind = classified.json().get("suggested_kind") or "fact"
            conflicting_kind = "decision" if suggested_kind != "decision" else "fact"
            failed = await client.post(
                "/v1/remember",
                json={
                    "content": content,
                    "classification_run_id": run_id,
                    "ingest_id": str(ingest_id),
                    "kind": conflicting_kind,
                },
                headers=_headers(ctx["broad_key"]),
            )
            assert failed.status_code in (409, 422), failed.text

            key_ids = [ctx["broad_key_id"], ctx["private_key_id"]]
            profile_ids = [ctx["broad_profile_id"], ctx["private_profile_id"]]
    finally:
        settings.auth_enabled = original_auth
        settings.embedding_provider = original_provider
        reset_principal_cache()
        if ingest_id is not None:
            await _cleanup_ingest(owner, ingest_id)
        if item_ids:
            await owner.execute("DELETE FROM memory_items WHERE id=ANY($1::uuid[])", item_ids)
        if key_ids:
            await owner.execute("DELETE FROM api_keys WHERE id=ANY($1::uuid[])", key_ids)
        if profile_ids:
            await owner.execute(
                "DELETE FROM memory_profiles WHERE id=ANY($1::uuid[])", profile_ids
            )
        await owner.close()


async def test_concurrent_first_consumption_serializes() -> None:
    """Two concurrent remember requests against one ingest under different
    profiles: exactly one wins and durably pins its context; the other receives
    a deterministic conflict. Exactly one execution row exists."""
    owner = await _owner()
    token = uuid.uuid4().hex[:10]
    original_auth = settings.auth_enabled
    original_provider = settings.embedding_provider
    ingest_id: uuid.UUID | None = None
    item_ids: list[uuid.UUID] = []
    key_ids: list[uuid.UUID] = []
    profile_ids: list[uuid.UUID] = []
    try:
        settings.auth_enabled = False
        settings.embedding_provider = "none"
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ctx = await _bootstrap(client, owner, token)

            settings.auth_enabled = True
            reset_principal_cache()

            content = f"concurrent consumption {token}"
            # Pre-create the ingest via a classify receipt so both concurrent
            # requests reuse the same ingest identity.
            classified = await client.post(
                "/v1/classify",
                json={"content": content},
                headers=_headers(ctx["broad_key"]),
            )
            assert classified.status_code == 200, classified.text
            ingest_id = uuid.UUID(classified.json()["ingest_id"])
            run_id = classified.json()["classification_run_id"]

            async def remember_with(key: str, profile_label: str) -> tuple[int, str]:
                resp = await client.post(
                    "/v1/remember",
                    json={"content": content, "classification_run_id": run_id},
                    headers=_headers(key),
                )
                return resp.status_code, profile_label

            # Fire two concurrent executions under different profiles. The
            # receipt can only bind once, so at most one can produce a 201; the
            # execution-context pin serializes the rest.
            results = await asyncio.gather(
                remember_with(ctx["broad_key"], "broad"),
                remember_with(ctx["private_key"], "private"),
            )
            statuses = {label: code for code, label in results}

            # At least one must succeed; any loser must get a bounded 4xx conflict.
            winners = [label for label, code in statuses.items() if code == 201]
            losers = [label for label, code in statuses.items() if code != 201]
            assert len(winners) == 1, f"expected exactly one winner, got {statuses}"
            for label in losers:
                assert statuses[label] in (409, 422), f"{label}: {statuses[label]}"

            row = await _execution_row(owner, ingest_id)
            assert row is not None, "no durable execution row after concurrent consumption"
            winner_profile = ctx[f"{winners[0]}_profile_id"]
            assert row["memory_profile_id"] == winner_profile

            exec_count = await owner.fetchval(
                "SELECT count(*) FROM candidate_ingest_executions WHERE ingest_id=$1",
                ingest_id,
            )
            assert exec_count == 1

            # Memory state is internally consistent: exactly one non-superseded
            # item for this content bound to the receipt.
            bound_item = await owner.fetchrow(
                "SELECT id, review_status FROM memory_items "
                "WHERE tenant_id=$1 AND content=$2 AND valid_to IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                ctx["tenant_id"],
                content,
            )
            assert bound_item is not None
            item_ids.append(bound_item["id"])

            key_ids = [ctx["broad_key_id"], ctx["private_key_id"]]
            profile_ids = [ctx["broad_profile_id"], ctx["private_profile_id"]]
    finally:
        settings.auth_enabled = original_auth
        settings.embedding_provider = original_provider
        reset_principal_cache()
        if ingest_id is not None:
            await _cleanup_ingest(owner, ingest_id)
        if item_ids:
            await owner.execute("DELETE FROM memory_items WHERE id=ANY($1::uuid[])", item_ids)
        if key_ids:
            await owner.execute("DELETE FROM api_keys WHERE id=ANY($1::uuid[])", key_ids)
        if profile_ids:
            await owner.execute(
                "DELETE FROM memory_profiles WHERE id=ANY($1::uuid[])", profile_ids
            )
        await owner.close()

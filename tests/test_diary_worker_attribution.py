# ruff: noqa: E501
"""Adversarial app-role proof for diary and worker attribution (P0-FIX-002B).

Unlike the older resolver-only tests, this module deliberately uses two database
roles.  The owner role is limited to corpus construction, hostile fixtures and
durable inspection.  HTTP requests and production worker/helper calls use the
non-owner application role with FORCE RLS.  All caller-facing requests use real
Bearer keys and the normal authentication dependencies.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    INTERNAL_DISPLAY_NAME_PREFIX,
    digest_api_key_secret,
    generate_api_key,
    hash_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings
from engram.conflicts import ConflictAction, ConflictResult, ConflictVerdict
from engram.db import apply_rls_context
from engram.internal_actors import (
    CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
    CONFLICT_AUTOMATION_INTERNAL_KEY,
    REVIEW_AUTOMATION_INTERNAL_KEY,
    InternalActorInvariantError,
    resolve_internal_system_actor,
)
from engram.models import Job
from engram.worker import (
    handle_classification_refine,
    handle_conflict_check,
    handle_embedding_generate,
)

DB_REASON = "requires owner and non-owner app PostgreSQL roles with the v2 schema"
INTERNAL_KEYS = (
    REVIEW_AUTOMATION_INTERNAL_KEY,
    CONFLICT_AUTOMATION_INTERNAL_KEY,
    CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
)


@dataclass(frozen=True)
class Actor:
    id: UUID
    name: str
    kind: str
    token: str


@dataclass(frozen=True)
class Corpus:
    tenant_a: UUID
    tenant_b: UUID
    actors: dict[str, Actor]


def _urls() -> tuple[str | None, str | None]:
    owner = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    return owner, os.getenv("ENGRAM_APP_DATABASE_URL")


async def _reachable(url: str | None) -> bool:
    if not url:
        return False
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def database():
    owner_url, app_url = _urls()
    if not await _reachable(owner_url) or not await _reachable(app_url):
        if os.getenv("ENGRAM_FAIL_ON_DB_SKIP") == "1":
            pytest.fail(DB_REASON)
        pytest.skip(DB_REASON)
    assert owner_url is not None and app_url is not None
    owner_engine = create_async_engine(owner_url, poolclass=NullPool)
    app_engine = create_async_engine(app_url, poolclass=NullPool)
    owner = async_sessionmaker(owner_engine, class_=AsyncSession, expire_on_commit=False)
    app = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)
    yield owner, app
    await app_engine.dispose()
    await owner_engine.dispose()


async def _issue(session: AsyncSession, tenant: UUID, principal: UUID, scopes: list[str]) -> str:
    token = generate_api_key()
    parsed = parse_api_key(token)
    assert parsed.key_id is not None
    await session.execute(
        text(
            "INSERT INTO api_keys (tenant_id, principal_id, key_hash, key_id, "
            "secret_digest, digest_algorithm, scopes, label) "
            "VALUES (:t, :p, NULL, :kid, :digest, :algorithm, :scopes, 'p0-fix-002b')"
        ),
        {
            "t": tenant,
            "p": principal,
            "kid": parsed.key_id,
            "digest": digest_api_key_secret(parsed.secret),
            "algorithm": DIGEST_ALGORITHM,
            "scopes": scopes,
        },
    )
    return token


@pytest.fixture(scope="module")
async def corpus(database) -> AsyncIterator[Corpus]:
    owner, _ = database
    tenant_a, tenant_b = uuid4(), uuid4()
    specs = {
        "user_a": (tenant_a, "User A", "user", ["read", "write"]),
        "user_b": (tenant_a, "User B", "user", ["read", "write"]),
        "admin_a": (tenant_a, "Admin A", "admin", ["read", "write", "admin"]),
        "admin_b": (tenant_a, "Admin B", "admin", ["read", "write"]),
        "agent_a": (tenant_a, "Agent A", "agent", ["read", "write"]),
        "agent_b": (tenant_a, "Agent B", "agent", ["read", "write"]),
        "system_a": (tenant_a, "System A", "system", ["read", "write"]),
        "system_b": (tenant_a, "System B", "system", ["read", "write"]),
        "named_system": (tenant_a, "system", "agent", ["read", "write"]),
        "named_review": (tenant_a, REVIEW_AUTOMATION_INTERNAL_KEY, "agent", ["read"]),
        "named_conflict": (tenant_a, CONFLICT_AUTOMATION_INTERNAL_KEY, "agent", ["read"]),
        "named_classification": (
            tenant_a,
            CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
            "agent",
            ["read"],
        ),
        "user_c": (tenant_b, "User C", "user", ["read", "write"]),
        "agent_c": (tenant_b, "Agent C", "agent", ["read", "write"]),
    }
    actors: dict[str, Actor] = {}
    async with owner() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:a,'P0 A',:sa),(:b,'P0 B',:sb)"),
            {"a": tenant_a, "b": tenant_b, "sa": f"p0-a-{tenant_a}", "sb": f"p0-b-{tenant_b}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_config (tenant_id, trust_manual_user, "
                "confidence_manual_user, trust_extraction, confidence_extraction) "
                "VALUES (:a,.83,.84,.43,.44),(:b,.73,.74,.33,.34)"
            ),
            {"a": tenant_a, "b": tenant_b},
        )
        for key, (tenant, name, kind, scopes) in specs.items():
            principal_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO principals (id, tenant_id, name, type) "
                    "VALUES (:id,:tenant,:name,:kind)"
                ),
                {"id": principal_id, "tenant": tenant, "name": name, "kind": kind},
            )
            token = await _issue(session, tenant, principal_id, scopes)
            actors[key] = Actor(principal_id, name, kind, token)
        await session.commit()
    try:
        yield Corpus(tenant_a, tenant_b, actors)
    finally:
        async with owner() as session:
            await session.execute(
                text(
                    "DELETE FROM item_events WHERE actor_principal_id IN "
                    "(SELECT id FROM principals WHERE tenant_id IN (:a,:b))"
                ),
                {"a": tenant_a, "b": tenant_b},
            )
            await session.execute(text("DELETE FROM tenants WHERE id IN (:a,:b)"), {"a": tenant_a, "b": tenant_b})
            await session.commit()


@pytest.fixture()
async def client(database, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    owner, app = database
    import engram.auth as auth_mod
    import engram.db as db_mod

    reset_principal_cache()
    monkeypatch.setattr(auth_mod, "_get_session_factory", lambda: owner)
    monkeypatch.setattr(db_mod, "async_session_factory", app)
    settings.auth_enabled = True
    transport = ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://engram.test") as http:
        yield http
    reset_principal_cache()


def _auth(actor: Actor) -> dict[str, str]:
    return {"Authorization": f"Bearer {actor.token}"}


async def _write(client: AsyncClient, actor: Actor, entry: str, **extra: object):
    return await client.post(
        "/v1/diary", headers=_auth(actor), json={"entry": entry, "topic": "proof", **extra}
    )


async def _inspect_item(owner, item_id: str):
    async with owner() as session:
        return (
            await session.execute(
                text(
                    "SELECT m.*, (SELECT count(*) FROM item_events e WHERE e.item_id=m.id) event_count "
                    "FROM memory_items m WHERE m.id=:id"
                ),
                {"id": item_id},
            )
        ).mappings().one()


@pytest.mark.asyncio
async def test_application_role_posture_force_rls_and_cross_tenant_isolation(database, corpus):
    owner, app = database
    protected = {"principals", "api_keys", "memory_items", "item_events", "jobs", "memory_embeddings"}
    async with app() as session:
        role = (await session.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert role == (False, False)
        forced = set(
            (await session.execute(text("SELECT relname FROM pg_class WHERE relname=ANY(:names) AND relforcerowsecurity"), {"names": list(protected)})).scalars()
        )
        assert forced == protected
        await apply_rls_context(session, tenant_id=corpus.tenant_a, principal_id=corpus.actors["user_a"].id)
        hidden = await session.execute(text("SELECT id FROM principals WHERE id=:id"), {"id": corpus.actors["user_c"].id})
        assert hidden.first() is None
    # Owner remains capable of durable inspection, proving this was RLS rather than absent data.
    async with owner() as session:
        assert (await session.execute(text("SELECT id FROM principals WHERE id=:id"), {"id": corpus.actors["user_c"].id})).scalar_one()


async def test_cross_principal_classification_receipt_is_non_disclosing_and_unmodified(
    database, corpus, client
):
    owner, _ = database
    content = "Cross-principal receipt access proof"
    classified = await client.post(
        "/v1/classify",
        headers=_auth(corpus.actors["user_a"]),
        json={"content": content, "source_type": "manual"},
    )
    assert classified.status_code == 200
    receipt_id = classified.json()["classification_run_id"]

    attempted = await client.post(
        "/v1/remember",
        headers=_auth(corpus.actors["user_b"]),
        json={
            "content": content,
            "source_type": "manual",
            "classification_run_id": receipt_id,
        },
    )
    assert attempted.status_code == 404
    async with owner() as session:
        run = (
            await session.execute(
                text(
                    "SELECT memory_item_id,bound_at FROM classification_runs WHERE id=:id"
                ),
                {"id": receipt_id},
            )
        ).one()
        item_count = await session.scalar(
            text("SELECT count(*) FROM memory_items WHERE content=:content"),
            {"content": content},
        )
        event_count = await session.scalar(
            text(
                "SELECT count(*) FROM item_events WHERE actor_principal_id=:principal"
            ),
            {"principal": corpus.actors["user_b"].id},
        )
    assert tuple(run) == (None, None)
    assert item_count == 0
    assert event_count == 0


@pytest.mark.asyncio
async def test_real_auth_self_write_matrix_and_configured_trust(client, database, corpus):
    owner, _ = database
    expected = {
        "user_a": ("manual", 50, "active", .83, .84),
        "admin_a": ("manual", 50, "active", .83, .84),
        "agent_a": ("extraction", 10, "proposed", .43, .44),
        "system_a": ("extraction", 10, "proposed", .43, .44),
    }
    for key, values in expected.items():
        actor = corpus.actors[key]
        response = await _write(client, actor, f"self matrix {key} {uuid4()}", reason="matrix")
        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["principal_id"], body["actor_principal_id"], body["represented"], body["attribution_status"]) == (str(actor.id), str(actor.id), False, "recorded")
        row = await _inspect_item(owner, body["id"])
        assert (row["source_type"], row["authority"], row["review_status"]) == values[:3]
        assert row["source_trust"] == pytest.approx(values[3])
        assert row["memory_confidence"] == pytest.approx(values[4])
        assert row["event_count"] == 1


@pytest.mark.asyncio
async def test_legacy_name_is_self_only_and_non_disclosing(client, database, corpus):
    owner, _ = database
    caller = corpus.actors["user_a"]
    ok = await _write(client, caller, f"legacy self {uuid4()}", principal=caller.name)
    assert ok.status_code == 201 and ok.json()["principal_id"] == str(caller.id)
    before: int
    async with owner() as session:
        before = (await session.execute(text("SELECT count(*) FROM memory_items WHERE tenant_id=:t"), {"t": corpus.tenant_a})).scalar_one()
    for foreign in (corpus.actors["user_b"].name, "absent principal", corpus.actors["user_c"].name):
        denied = await _write(client, caller, f"legacy denied {uuid4()}", principal=foreign)
        assert denied.status_code == 422
        assert denied.json() == {"detail": "legacy principal must identify the caller"}
    mixed = await _write(client, caller, "ambiguous", principal=caller.name, on_behalf_of_principal_id=str(corpus.actors["user_b"].id))
    assert mixed.status_code == 422
    async with owner() as session:
        after = (await session.execute(text("SELECT count(*) FROM memory_items WHERE tenant_id=:t"), {"t": corpus.tenant_a})).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_representation_authorization_and_non_disclosure(client, database, corpus):
    owner, app = database
    admin = corpus.actors["admin_a"]
    for target_key in ("user_b", "agent_b", "system_b"):
        target = corpus.actors[target_key]
        response = await _write(client, admin, f"represented {target_key} {uuid4()}", on_behalf_of_principal_id=str(target.id), reason="oversight")
        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["principal_id"], body["actor_principal_id"], body["represented"]) == (str(target.id), str(admin.id), True)
        row = await _inspect_item(owner, body["id"])
        assert (row["source_type"], row["authority"], row["review_status"], row["event_count"]) == ("manual", 50, "active", 1)
    target = corpus.actors["user_b"]
    for key in ("user_a", "agent_a", "system_a", "admin_b"):
        denied = await _write(client, corpus.actors[key], f"denied {uuid4()}", on_behalf_of_principal_id=str(target.id))
        assert denied.status_code == 403
    async with app() as session:
        await apply_rls_context(session, tenant_id=corpus.tenant_a, principal_id=admin.id)
        internal_ids = [str(await resolve_internal_system_actor(session, tenant_id=corpus.tenant_a, internal_key=key)) for key in INTERNAL_KEYS]
        await session.commit()
    for target_id in (str(uuid4()), str(corpus.actors["user_c"].id), *internal_ids):
        hidden = await _write(client, admin, f"hidden {uuid4()}", on_behalf_of_principal_id=target_id)
        assert hidden.status_code == 404 and hidden.json() == {"detail": "principal not found"}


@pytest.mark.asyncio
async def test_modern_event_truth_and_unique_index_dedup_preserve_original_actor(client, database, corpus):
    owner, _ = database
    admin = corpus.actors["admin_a"]
    target = corpus.actors["agent_b"]
    content = f"modern represented dedup {uuid4()}"
    first = await _write(client, admin, content, on_behalf_of_principal_id=str(target.id), reason="original reason")
    assert first.status_code == 201
    retry = await _write(client, admin, content, on_behalf_of_principal_id=str(target.id), reason="retry must vanish")
    assert retry.status_code == 201
    assert retry.json() | {"status": "deduped"} == retry.json()
    assert retry.json()["status"] == "deduped"
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["actor_principal_id"] == str(admin.id)
    async with owner() as session:
        events = (await session.execute(text("SELECT * FROM item_events WHERE item_id=:id"), {"id": first.json()["id"]})).mappings().all()
        assert len(events) == 1
        event = events[0]
        details = json.loads(event["new_value"])
        assert event["event_type"] == "diary_create"
        assert event["field_name"] == "principal_id"
        assert event["actor_principal_id"] == admin.id
        assert event["reason"] == "original reason"
        assert details == {
            "actor_principal_id": str(admin.id), "authority": 50, "authority_label": "explicit_user",
            "memory_confidence": pytest.approx(.84), "on_behalf_of_principal_id": str(target.id),
            "owner_principal_id": str(target.id), "represented": True, "review_status": "active",
            "source_trust": pytest.approx(.83), "source_type": "manual", "topic": "proof",
        }


@pytest.mark.asyncio
async def test_legacy_dedup_stays_unknown_and_event_free(client, database, corpus):
    owner, _ = database
    for target_key, caller_key in (("user_b", "user_b"), ("agent_b", "agent_b"), ("system_b", "system_b")):
        target, caller = corpus.actors[target_key], corpus.actors[caller_key]
        content = f"pre-event legacy {target_key} {uuid4()}"
        from engram.canonicalize import canonicalize, content_hash
        item_id = uuid4()
        async with owner() as session:
            await session.execute(text("INSERT INTO memory_items (id,tenant_id,principal_id,content,content_hash,kind,subject_name,visibility,review_status,memory_confidence,source_trust,importance,source_type,authority,sensitivity) VALUES (:id,:t,:p,:c,:h,'diary_entry','proof','private','active',.91,.92,.4,'manual',50,'normal')"), {"id": item_id, "t": corpus.tenant_a, "p": target.id, "c": content, "h": content_hash(canonicalize(content))})
            await session.commit()
        for _ in range(2):
            response = await _write(client, caller, content)
            assert response.status_code == 201
            assert response.json()["id"] == str(item_id)
            assert (response.json()["status"], response.json()["actor_principal_id"], response.json()["represented"], response.json()["attribution_status"]) == ("deduped", None, None, "legacy_unknown")
        row = await _inspect_item(owner, str(item_id))
        assert row["event_count"] == 0 and row["source_trust"] == pytest.approx(.92)


@pytest.mark.asyncio
async def test_malformed_modern_dedup_fails_closed(client, database, corpus):
    owner, _ = database
    actor = corpus.actors["user_a"]
    content = f"malformed modern {uuid4()}"
    created = await _write(client, actor, content)
    assert created.status_code == 201
    async with owner() as session:
        await session.execute(text("UPDATE item_events SET new_value='not-json' WHERE item_id=:id"), {"id": created.json()["id"]})
        await session.commit()
    retry = await _write(client, actor, content)
    assert retry.status_code == 500
    row = await _inspect_item(owner, created.json()["id"])
    assert row["event_count"] == 1 and row["principal_id"] == actor.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation",
    [
        "duplicate_event",
        "null_actor",
        "invalid_json",
        "owner_mismatch",
        "actor_mismatch",
        "represented_missing_target",
        "self_with_target",
        "authority_mismatch",
        "authority_label_mismatch",
        "source_type_mismatch",
        "trust_confidence_mismatch",
        "review_state_mismatch",
    ],
)
async def test_complete_malformed_modern_matrix_fails_closed(
    client, database, corpus, malformation
):
    owner, _ = database
    actor = corpus.actors["user_a"]
    content = f"malformed {malformation} {uuid4()}"
    created = await _write(client, actor, content)
    assert created.status_code == 201
    item_id = created.json()["id"]
    async with owner() as session:
        event = (
            await session.execute(
                text("SELECT id,new_value FROM item_events WHERE item_id=:id"),
                {"id": item_id},
            )
        ).mappings().one()
        details = json.loads(event["new_value"])
        if malformation == "duplicate_event":
            await session.execute(
                text(
                    "INSERT INTO item_events "
                    "(item_id,event_type,field_name,new_value,actor_principal_id) "
                    "SELECT item_id,event_type,field_name,new_value,actor_principal_id "
                    "FROM item_events WHERE id=:event"
                ),
                {"event": event["id"]},
            )
        elif malformation == "null_actor":
            await session.execute(
                text("UPDATE item_events SET actor_principal_id=NULL WHERE id=:event"),
                {"event": event["id"]},
            )
        elif malformation == "invalid_json":
            await session.execute(
                text("UPDATE item_events SET new_value='not-json' WHERE id=:event"),
                {"event": event["id"]},
            )
        else:
            if malformation == "owner_mismatch":
                details["owner_principal_id"] = str(corpus.actors["user_b"].id)
            elif malformation == "actor_mismatch":
                details["actor_principal_id"] = str(corpus.actors["user_b"].id)
            elif malformation == "represented_missing_target":
                details["represented"] = True
                details["on_behalf_of_principal_id"] = None
            elif malformation == "self_with_target":
                details["on_behalf_of_principal_id"] = str(actor.id)
            elif malformation == "authority_mismatch":
                details["authority"] = 10
            elif malformation == "authority_label_mismatch":
                details["authority_label"] = "inferred"
            elif malformation == "source_type_mismatch":
                details["source_type"] = "extraction"
            elif malformation == "trust_confidence_mismatch":
                details["source_trust"] = .01
                details["memory_confidence"] = .02
            elif malformation == "review_state_mismatch":
                details["review_status"] = "proposed"
            await session.execute(
                text("UPDATE item_events SET new_value=:value WHERE id=:event"),
                {"value": json.dumps(details), "event": event["id"]},
            )
        await session.commit()
    retry = await _write(client, actor, content)
    assert retry.status_code == 500
    async with owner() as session:
        durable = (
            await session.execute(
                text(
                    "SELECT principal_id,source_type,review_status FROM memory_items "
                    "WHERE id=:id"
                ),
                {"id": item_id},
            )
        ).one()
        event_count = (
            await session.execute(
                text("SELECT count(*) FROM item_events WHERE item_id=:id"), {"id": item_id}
            )
        ).scalar_one()
    assert durable == (actor.id, "manual", "active")
    assert event_count == (2 if malformation == "duplicate_event" else 1)


@pytest.mark.asyncio
async def test_diary_event_failure_rolls_back_memory_and_recovers(client, database, corpus):
    owner, _ = database
    actor = corpus.actors["user_a"]
    content = f"rollback atomicity {uuid4()}"
    async with owner() as session:
        await session.execute(text("CREATE FUNCTION p0_reject_diary_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_type='diary_create' THEN RAISE EXCEPTION 'p0 reject'; END IF; RETURN NEW; END $$"))
        await session.execute(text("CREATE TRIGGER p0_reject_diary_event BEFORE INSERT ON item_events FOR EACH ROW EXECUTE FUNCTION p0_reject_diary_event()"))
        await session.commit()
    try:
        failed = await _write(client, actor, content)
        assert failed.status_code == 500
        async with owner() as session:
            assert (await session.execute(text("SELECT count(*) FROM memory_items WHERE content=:c"), {"c": content})).scalar_one() == 0
    finally:
        async with owner() as session:
            await session.execute(text("DROP TRIGGER IF EXISTS p0_reject_diary_event ON item_events"))
            await session.execute(text("DROP FUNCTION IF EXISTS p0_reject_diary_event()"))
            await session.commit()
    assert (await _write(client, actor, content)).status_code == 201


@pytest.mark.asyncio
async def test_diary_memory_failure_leaves_no_event_and_recovers(client, database, corpus):
    owner, _ = database
    actor = corpus.actors["user_a"]
    content = f"memory rollback atomicity {uuid4()}"
    async with owner() as session:
        await session.execute(
            text(
                "CREATE FUNCTION p0_reject_diary_memory() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN IF NEW.kind='diary_entry' THEN RAISE EXCEPTION 'p0 reject memory'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER p0_reject_diary_memory BEFORE INSERT ON memory_items "
                "FOR EACH ROW EXECUTE FUNCTION p0_reject_diary_memory()"
            )
        )
        await session.commit()
    try:
        failed = await _write(client, actor, content)
        assert failed.status_code == 500
        async with owner() as session:
            assert (
                await session.execute(
                    text("SELECT count(*) FROM memory_items WHERE content=:content"),
                    {"content": content},
                )
            ).scalar_one() == 0
            assert (
                await session.execute(
                    text("SELECT count(*) FROM item_events WHERE reason='memory rollback'")
                )
            ).scalar_one() == 0
    finally:
        async with owner() as session:
            await session.execute(
                text("DROP TRIGGER IF EXISTS p0_reject_diary_memory ON memory_items")
            )
            await session.execute(text("DROP FUNCTION IF EXISTS p0_reject_diary_memory()"))
            await session.commit()
    recovered = await _write(client, actor, content, reason="memory rollback")
    assert recovered.status_code == 201


@pytest.mark.asyncio
async def test_diary_read_policy_and_deterministic_tiebreak(client, database, corpus):
    owner, _ = database
    target = corpus.actors["agent_b"]
    ids = []
    for suffix in ("low", "high"):
        response = await _write(client, target, f"ordered {suffix} {uuid4()}")
        assert response.status_code == 201
        ids.append(response.json()["id"])
    async with owner() as session:
        await session.execute(text("UPDATE memory_items SET created_at='2026-01-01T00:00:00Z' WHERE id=ANY(:ids)"), {"ids": ids})
        await session.commit()
    for key in ("agent_b", "user_a", "admin_a"):
        response = await client.get(f"/v1/diary/{target.name}", headers=_auth(corpus.actors[key]))
        assert response.status_code == 200
        returned = [row["id"] for row in response.json() if row["id"] in ids]
        assert returned == sorted(ids, reverse=True)
    for key in ("agent_a", "system_a"):
        assert (await client.get(f"/v1/diary/{target.name}", headers=_auth(corpus.actors[key]))).status_code == 403
    assert (await client.get(f"/v1/diary/{corpus.actors['user_c'].name}", headers=_auth(corpus.actors["user_a"]))).status_code == 404
    assert (await client.get("/v1/diary/__engram_internal__:absent", headers=_auth(corpus.actors["admin_a"]))).status_code == 404


@pytest.mark.asyncio
async def test_internal_actor_identity_concurrency_and_unknown_key(database, corpus):
    owner, app = database
    async def resolve(key: str) -> UUID:
        async with app() as session:
            await apply_rls_context(session, tenant_id=corpus.tenant_a, principal_id=corpus.actors["admin_a"].id)
            value = await resolve_internal_system_actor(session, tenant_id=corpus.tenant_a, internal_key=key)
            await session.commit()
            return value
    resolved = {key: await resolve(key) for key in INTERNAL_KEYS}
    assert len(set(resolved.values())) == 3
    async with app() as session:
        await apply_rls_context(
            session,
            tenant_id=corpus.tenant_b,
            principal_id=corpus.actors["user_c"].id,
        )
        tenant_b_ids = {
            key: await resolve_internal_system_actor(
                session, tenant_id=corpus.tenant_b, internal_key=key
            )
            for key in INTERNAL_KEYS
        }
        await session.commit()
    assert all(tenant_b_ids[key] != resolved[key] for key in INTERNAL_KEYS)
    for key in (CONFLICT_AUTOMATION_INTERNAL_KEY, CLASSIFICATION_AUTOMATION_INTERNAL_KEY):
        values = await asyncio.gather(*(resolve(key) for _ in range(6)))
        assert len(set(values)) == 1
    async with owner() as session:
        rows = (await session.execute(text("SELECT id,name,type,internal_key FROM principals WHERE tenant_id=:t AND internal_key IS NOT NULL"), {"t": corpus.tenant_a})).mappings().all()
        assert {(row["id"], row["internal_key"]) for row in rows} == {(value, key) for key, value in resolved.items()}
        assert all(
            row["type"] == "system"
            and row["name"].startswith(f"{INTERNAL_DISPLAY_NAME_PREFIX}:")
            for row in rows
        )
        assert all(corpus.actors[f"named_{key.split('_')[0]}"].id != resolved[key] for key in INTERNAL_KEYS)
    async with app() as session:
        await apply_rls_context(session, tenant_id=corpus.tenant_a, principal_id=corpus.actors["admin_a"].id)
        with pytest.raises(InternalActorInvariantError, match="unsupported"):
            await resolve_internal_system_actor(session, tenant_id=corpus.tenant_a, internal_key="unknown")


@pytest.mark.asyncio
async def test_direct_new_and_legacy_keys_cannot_credential_internal_actor(client, database, corpus):
    owner, _ = database
    async with owner() as session:
        internal_id = (await session.execute(text("SELECT id FROM principals WHERE tenant_id=:t AND internal_key=:k"), {"t": corpus.tenant_a, "k": REVIEW_AUTOMATION_INTERNAL_KEY})).scalar_one()
        new_token = await _issue(session, corpus.tenant_a, internal_id, ["read", "write", "admin"])
        legacy_token = f"eng_{uuid4().hex}"
        await session.execute(text("INSERT INTO api_keys (tenant_id,principal_id,key_hash,key_id,secret_digest,digest_algorithm,scopes,label) VALUES (:t,:p,:hash,NULL,NULL,NULL,:scopes,'hostile legacy')"), {"t": corpus.tenant_a, "p": internal_id, "hash": hash_api_key(legacy_token), "scopes": ["read", "write", "admin"]})
        await session.commit()
    for token in (new_token, legacy_token):
        response = await client.get("/v1/diary/User A", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401 and response.json()["detail"] == "Invalid or revoked API key"


@pytest.mark.asyncio
async def test_admin_issuance_and_cached_internal_principal_fail_closed(
    client, database, corpus
):
    owner, _ = database
    import engram.auth as auth_mod

    async with owner() as session:
        internal = (
            await session.execute(
                text(
                    "SELECT id,internal_key FROM principals "
                    "WHERE tenant_id=:t AND internal_key IS NOT NULL"
                ),
                {"t": corpus.tenant_a},
            )
        ).mappings().all()
    assert {row["internal_key"] for row in internal} == set(INTERNAL_KEYS)
    for row in internal:
        response = await client.post(
            "/v1/admin/api-keys",
            headers=_auth(corpus.actors["admin_a"]),
            json={
                "tenant_id": str(corpus.tenant_a),
                "principal_id": str(row["id"]),
                "scopes": ["read"],
            },
        )
        assert response.status_code == 409

    token = generate_api_key()
    parsed = parse_api_key(token)
    assert parsed.key_id is not None
    auth_mod._principal_cache.put(
        parsed.key_id,
        digest_api_key_secret(parsed.secret),
        auth_mod.Principal(
            tenant_id=str(corpus.tenant_a),
            principal_id=str(internal[0]["id"]),
            scopes=("read", "write", "admin"),
            internal_key=str(internal[0]["internal_key"]),
        ),
    )
    cached = await client.get(
        "/v1/diary/User A", headers={"Authorization": f"Bearer {token}"}
    )
    assert cached.status_code == 401


async def _worker_item(
    owner,
    corpus: Corpus,
    *,
    author: str,
    content: str,
    created_at: datetime | None = None,
    review_status: str = "active",
    valid_to: datetime | None = None,
    superseded_by: UUID | None = None,
    authority: int = 10,
) -> UUID:
    item_id = uuid4()
    async with owner() as session:
        await session.execute(
            text(
                "INSERT INTO memory_items "
                "(id,tenant_id,principal_id,content,content_hash,kind,visibility,review_status,"
                "memory_confidence,source_trust,importance,source_type,authority,sensitivity,"
                "created_at,valid_to,superseded_by) VALUES "
                "(:id,:tenant,:principal,:content,:hash,'fact','workspace',:review,.2,.9,.4,"
                "'extraction',:authority,'normal',:created,:valid_to,:superseded_by)"
            ),
            {
                "id": item_id,
                "tenant": corpus.tenant_a,
                "principal": corpus.actors[author].id,
                "content": content,
                "hash": f"sha256:{uuid4().hex}",
                "review": review_status,
                "authority": authority,
                "created": created_at or datetime.now(UTC),
                "valid_to": valid_to,
                "superseded_by": superseded_by,
            },
        )
        await session.commit()
    return item_id


def _job(corpus: Corpus, job_type: str, item_id: UUID) -> Job:
    return Job(
        id=uuid4(),
        tenant_id=corpus.tenant_a,
        job_type=job_type,
        status="running",
        payload={"memory_item_id": str(item_id)},
    )


async def _app_session(app, corpus: Corpus) -> AsyncIterator[AsyncSession]:
    async with app() as session:
        await apply_rls_context(
            session,
            tenant_id=corpus.tenant_a,
            principal_id=corpus.actors["admin_a"].id,
        )
        yield session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        ConflictAction.DEDUP,
        ConflictAction.AUTO_SUPERSEDE,
        ConflictAction.FLAG_CONTRADICTION,
        ConflictAction.FLAG_SCOPE_OVERLAP,
        ConflictAction.PROPOSED_SUPERSEDE,
    ],
)
@pytest.mark.parametrize("author", ["agent_a", "system_a"])
async def test_every_conflict_branch_uses_conflict_actor(
    database, corpus, monkeypatch: pytest.MonkeyPatch, action: ConflictAction, author: str
):
    owner, app = database
    # AUTO_SUPERSEDE requires the new item to qualify for automatic supersession
    # (authority >= trusted_import=40) and to out-rank the old item. DEDUP and
    # flagging actions do not depend on authority, so keep the default (inferred=
    # 10) for those to preserve the original attribution fixture shape.
    new_authority = 40 if action == ConflictAction.AUTO_SUPERSEDE else 10
    old_authority = 10
    old_id = await _worker_item(
        owner,
        corpus,
        author=author,
        content=f"old {action} {uuid4()}",
        created_at=datetime.now(UTC) - timedelta(days=1),
        authority=old_authority,
    )
    new_id = await _worker_item(
        owner,
        corpus,
        author=author,
        content=f"new {action} {uuid4()}",
        created_at=datetime.now(UTC),
        authority=new_authority,
    )
    job = _job(corpus, "conflict.check", new_id)

    # P0-FIX-004C2: the DEDUP branch now revalidates the detector's embedding
    # eligibility under the pair locks (the embedding job can flip an
    # embedding's readiness without taking the item row lock). This attribution
    # test mocks detection entirely, so it must provide the ready embedding rows
    # and a profile with ``dimensions`` that production detection always
    # implies. Reuse the real seeded active profile so the embedding FK is
    # satisfied, and mirror it in the mock.
    async with owner() as session:
        prof = (
            await session.execute(
                text(
                    "SELECT id, profile_key, dimensions, state, provider, model, "
                    "distance_metric FROM embedding_profiles "
                    "WHERE state='active' LIMIT 1"
                )
            )
        ).one()
    profile = SimpleNamespace(
        id=prof[0],
        profile_key=prof[1],
        state=prof[3],
        dimensions=int(prof[2]),
        provider=prof[4],
        model=prof[5],
        distance_metric=prof[6],
    )

    async def active_profile(_session):
        return profile

    async def verdict(_item, _session, *, profile, **_kwargs):
        del profile
        return ConflictResult(
            verdict=(ConflictVerdict.DUPLICATE if action == ConflictAction.DEDUP else ConflictVerdict.REFINE),
            action=action,
            existing_item_id=old_id,
            similarity=.99,
            classifier_confidence=.99,
            conflict_type="scope_overlap" if action == ConflictAction.FLAG_SCOPE_OVERLAP else "contradiction",
            reason="forced branch",
            provenance={"provider": "test", "classifier": "forced"},
        )

    # Insert ready embeddings for both items matching the active profile so the
    # DEDUP branch's embedding revalidation passes (production detection always
    # requires both items to carry a ready embedding for the profile).
    dim = profile.dimensions
    vec = "[" + ",".join(["1"] + ["0"] * (dim - 1)) + "]"
    async with owner() as session:
        for iid in (old_id, new_id):
            await session.execute(
                text(
                    "INSERT INTO memory_embeddings "
                    "(memory_item_id, tenant_id, profile_id, embedding_model, embedding_dim, "
                    "embedding, embedding_status) VALUES "
                    "(:id, :tenant, :profile_id, 'test', :dim, CAST(:vec AS vector), 'ready')"
                ),
                {
                    "id": iid,
                    "tenant": corpus.tenant_a,
                    "profile_id": profile.id,
                    "dim": dim,
                    "vec": vec,
                },
            )
        await session.commit()

    monkeypatch.setattr("engram.embedding_profiles.get_active_profile", active_profile)
    monkeypatch.setattr("engram.conflicts.detect_conflicts", verdict)
    async for session in _app_session(app, corpus):
        await handle_conflict_check(session, job)

    # P0-FIX-004D: AUTO_SUPERSEDE mutates the OLD row (sets superseded_by) and
    # attaches its truthful event to the OLD item. Every other conflict branch
    # mutates and records its event on the NEW/job item.
    event_item_id = old_id if action == ConflictAction.AUTO_SUPERSEDE else new_id
    async with owner() as session:
        event = (
            await session.execute(
                text(
                    "SELECT event_type, field_name, old_value, new_value, "
                    "actor_principal_id, reason FROM item_events WHERE item_id=:id"
                ),
                {"id": event_item_id},
            )
        ).mappings().one()
        actor = (
            await session.execute(
                text("SELECT id FROM principals WHERE tenant_id=:t AND internal_key=:key"),
                {"t": corpus.tenant_a, "key": CONFLICT_AUTOMATION_INTERNAL_KEY},
            )
        ).scalar_one()
    provenance = json.loads(event["reason"])
    assert event["actor_principal_id"] == actor
    assert actor != corpus.actors[author].id
    assert provenance["worker_operation"] == "conflict.check"
    assert provenance["job_id"] == str(job.id)
    assert provenance["item_author_principal_id"] == str(corpus.actors[author].id)
    assert provenance["internal_actor_key"] == CONFLICT_AUTOMATION_INTERNAL_KEY
    assert provenance["action"] == action.value
    assert provenance["existing_item_id"] == str(old_id)
    # Detector provenance: AUTO_SUPERSEDE namespaces untrusted provider data
    # under ``detector_provenance`` so it cannot overwrite canonical fields;
    # the other conflict branches still merge provenance at the top level.
    if action == ConflictAction.AUTO_SUPERSEDE:
        assert provenance["detector_provenance"]["provider"] == "test"
    else:
        assert provenance["provider"] == "test"
    # P0-FIX-004D: AUTO_SUPERSEDE event describes the mutated old row
    # unambiguously — it records the old/new item roles and the actual
    # superseded_by value (the new item id), and the event row carries the
    # truthful field_name/old_value/new_value on the mutated OLD item.
    if action == ConflictAction.AUTO_SUPERSEDE:
        assert provenance["old_item_id"] == str(old_id)
        assert provenance["new_item_id"] == str(new_id)
        assert event["event_type"] == "conflict_detected"
        assert event["field_name"] == "superseded_by"
        assert event["old_value"] is None
        assert event["new_value"] == str(new_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("author", ["agent_a", "system_a"])
async def test_classification_change_and_no_change_use_classification_actor(
    database, corpus, monkeypatch: pytest.MonkeyPatch, author: str
):
    from engram.classification import ClassificationResult

    owner, app = database
    item_id = await _worker_item(owner, corpus, author=author, content=f"classify {uuid4()}")
    job = _job(corpus, "classification.refine", item_id)

    async def changed(*_args, **_kwargs):
        return ClassificationResult(
            suggested_kind="decision",
            suggested_wing="project",
            suggested_room="backlog",
            suggested_visibility="private",
            confidence=.9,
            reason="forced changes",
            provenance={"provider": "test"},
        )

    monkeypatch.setattr("engram.classification.classify", changed)
    async for session in _app_session(app, corpus):
        await handle_classification_refine(session, job)

    no_change_id = await _worker_item(owner, corpus, author=author, content=f"no change {uuid4()}")
    no_change_job = _job(corpus, "classification.refine", no_change_id)

    async def unchanged(*_args, **_kwargs):
        return ClassificationResult(
            suggested_kind="fact", confidence=.1, reason="stable", provenance={"provider": "test"}
        )

    monkeypatch.setattr("engram.classification.classify", unchanged)
    async for session in _app_session(app, corpus):
        await handle_classification_refine(session, no_change_job)

    async with owner() as session:
        actor = (
            await session.execute(
                text("SELECT id FROM principals WHERE tenant_id=:t AND internal_key=:key"),
                {"t": corpus.tenant_a, "key": CLASSIFICATION_AUTOMATION_INTERNAL_KEY},
            )
        ).scalar_one()
        rows = (
            await session.execute(
                text(
                    "SELECT item_id,actor_principal_id,reason FROM item_events "
                    "WHERE item_id IN (:changed,:unchanged) ORDER BY created_at,id"
                ),
                {"changed": item_id, "unchanged": no_change_id},
            )
        ).mappings().all()
    assert {row["item_id"] for row in rows} == {item_id, no_change_id}
    assert all(row["actor_principal_id"] == actor for row in rows)
    assert actor != corpus.actors[author].id
    for row in rows:
        provenance = json.loads(row["reason"])
        expected_job = job if row["item_id"] == item_id else no_change_job
        assert provenance["worker_operation"] == "classification.refine"
        assert provenance["job_id"] == str(expected_job.id)
        assert provenance["item_author_principal_id"] == str(corpus.actors[author].id)
        assert provenance["internal_actor_key"] == CLASSIFICATION_AUTOMATION_INTERNAL_KEY


@pytest.mark.asyncio
async def test_path_a_promotion_remains_review_automation(database, corpus):
    from engram.promotion import auto_promote_proposed_memories

    owner, app = database
    item_id = await _worker_item(
        owner,
        corpus,
        author="agent_a",
        content=f"eligible promotion {uuid4()}",
        created_at=datetime.now(UTC) - timedelta(days=5),
        review_status="proposed",
    )
    async with owner() as session:
        await session.execute(
            text("UPDATE memory_items SET memory_confidence=.95 WHERE id=:id"),
            {"id": item_id},
        )
        await session.commit()
    async for session in _app_session(app, corpus):
        result = await auto_promote_proposed_memories(
            session, str(corpus.tenant_a), source="worker"
        )
    assert item_id in result.promoted_ids
    async with owner() as session:
        event = (
            await session.execute(
                text(
                    "SELECT e.actor_principal_id,p.internal_key FROM item_events e "
                    "JOIN principals p ON p.id=e.actor_principal_id "
                    "WHERE e.item_id=:id AND e.event_type='review_change'"
                ),
                {"id": item_id},
            )
        ).mappings().one()
    assert event["internal_key"] == REVIEW_AUTOMATION_INTERNAL_KEY
    assert event["actor_principal_id"] != corpus.actors["agent_a"].id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["archived", "rejected", "invalidated", "superseded"])
@pytest.mark.parametrize(
    ("job_type", "handler"),
    [
        ("embedding.generate", handle_embedding_generate),
        ("conflict.check", handle_conflict_check),
        ("classification.refine", handle_classification_refine),
    ],
)
async def test_terminal_handlers_skip_provider_events_and_actor_creation(
    database, corpus, monkeypatch: pytest.MonkeyPatch, state, job_type, handler
):
    owner, app = database
    kwargs: dict[str, object] = {"review_status": "active"}
    if state in {"archived", "rejected"}:
        kwargs["review_status"] = state
    elif state == "invalidated":
        kwargs["valid_to"] = datetime.now(UTC)
    else:
        kwargs["superseded_by"] = await _worker_item(
            owner, corpus, author="agent_a", content=f"superseder {uuid4()}"
        )
    item_id = await _worker_item(
        owner, corpus, author="agent_a", content=f"terminal {state} {job_type} {uuid4()}", **kwargs
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider/classifier must not run for a terminal item")

    monkeypatch.setattr("engram.embeddings.generate_embedding", forbidden)
    monkeypatch.setattr("engram.conflicts.detect_conflicts", forbidden)
    monkeypatch.setattr("engram.classification.classify", forbidden)
    key = {
        "conflict.check": CONFLICT_AUTOMATION_INTERNAL_KEY,
        "classification.refine": CLASSIFICATION_AUTOMATION_INTERNAL_KEY,
    }.get(job_type)
    async with owner() as session:
        before_actors = (
            await session.execute(
                text("SELECT count(*) FROM principals WHERE tenant_id=:t AND internal_key=:key"),
                {"t": corpus.tenant_a, "key": key},
            )
        ).scalar_one() if key else 0
    async for session in _app_session(app, corpus):
        await handler(session, _job(corpus, job_type, item_id))
    async with owner() as session:
        event_count = (
            await session.execute(text("SELECT count(*) FROM item_events WHERE item_id=:id"), {"id": item_id})
        ).scalar_one()
        embedding_count = (
            await session.execute(text("SELECT count(*) FROM memory_embeddings WHERE memory_item_id=:id"), {"id": item_id})
        ).scalar_one()
        after_actors = (
            await session.execute(
                text("SELECT count(*) FROM principals WHERE tenant_id=:t AND internal_key=:key"),
                {"t": corpus.tenant_a, "key": key},
            )
        ).scalar_one() if key else 0
    assert event_count == 0
    assert embedding_count == 0
    assert after_actors == before_actors


@pytest.mark.asyncio
async def test_disputed_item_is_not_terminal_control(
    database, corpus, monkeypatch: pytest.MonkeyPatch
):
    from engram.classification import ClassificationResult

    owner, app = database
    item_id = await _worker_item(
        owner,
        corpus,
        author="agent_a",
        content=f"disputed control {uuid4()}",
        review_status="disputed",
    )
    called = False

    async def classify_control(*_args, **_kwargs):
        nonlocal called
        called = True
        return ClassificationResult(
            suggested_kind="fact", confidence=.1, reason="control", provenance={"provider": "test"}
        )

    monkeypatch.setattr("engram.classification.classify", classify_control)
    async for session in _app_session(app, corpus):
        await handle_classification_refine(
            session, _job(corpus, "classification.refine", item_id)
        )
    assert called
    async with owner() as session:
        event = (
            await session.execute(
                text("SELECT actor_principal_id FROM item_events WHERE item_id=:id"),
                {"id": item_id},
            )
        ).scalar_one()
        actor_key = (
            await session.execute(
                text("SELECT internal_key FROM principals WHERE id=:id"), {"id": event}
            )
        ).scalar_one()
    assert actor_key == CLASSIFICATION_AUTOMATION_INTERNAL_KEY


def test_worker_source_guard_does_not_assign_author_as_actor():
    source = (Path(__file__).parents[1] / "engram" / "worker.py").read_text()
    forbidden = re.compile(r"(?:actor_principal_id|\bactor)\s*=\s*item\.principal_id")
    assert forbidden.search(source) is None
    predicate = re.search(r"def _is_expired_or_inactive\(.*?(?=\n\nasync def)", source, re.S)
    assert predicate is not None and 'item.review_status == "archived"' in predicate.group()

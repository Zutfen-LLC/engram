"""Focused real-PostgreSQL integrity proof for canonical feedback."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    generate_api_key,
    parse_api_key,
    reset_principal_cache,
)
from engram.config import settings
from engram.db import apply_rls_context
from engram.feedback import FeedbackRateLimitError, record_feedback

_owner_engine = create_async_engine(
    settings.owner_database_url or settings.database_url, poolclass=NullPool
)
_owner_factory = async_sessionmaker(_owner_engine, class_=AsyncSession, expire_on_commit=False)
_app_url = os.environ.get("ENGRAM_APP_DATABASE_URL")
_app_engine = create_async_engine(_app_url, poolclass=NullPool) if _app_url else None
_app_factory = (
    async_sessionmaker(_app_engine, class_=AsyncSession, expire_on_commit=False)
    if _app_engine is not None
    else None
)


async def _require_stack() -> None:
    if _app_factory is None:
        pytest.skip("requires a live PostgreSQL with the non-owner app role")
    try:
        async with _owner_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with _app_engine.connect() as conn:  # type: ignore[union-attr]
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("requires a live PostgreSQL with the non-owner app role")


@pytest.fixture
async def tenant() -> dict[str, Any]:
    await _require_stack()
    tenant_id = uuid.uuid4()
    slug = f"feedback-integrity-{tenant_id.hex}"
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": slug, "slug": slug},
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_config "
                "(tenant_id, config_version, active, feedback_daily_limit) "
                "VALUES (:id, 'feedback-integrity', TRUE, 500)"
            ),
            {"id": tenant_id},
        )
    data: dict[str, Any] = {"id": tenant_id, "principals": [], "items": []}
    try:
        yield data
    finally:
        reset_principal_cache()
        async with _owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})


async def _principal(tenant: dict[str, Any], principal_type: str) -> uuid.UUID:
    principal_id = uuid.uuid4()
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type) "
                "VALUES (:id, :tid, :name, :type)"
            ),
            {
                "id": principal_id,
                "tid": tenant["id"],
                "name": f"feedback-{principal_type}-{principal_id}",
                "type": principal_type,
            },
        )
    tenant["principals"].append(principal_id)
    return principal_id


async def _item(
    tenant: dict[str, Any],
    author_id: uuid.UUID,
    *,
    importance: float = 0.5,
    visibility: str = "tenant",
) -> uuid.UUID:
    item_id = uuid.uuid4()
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO memory_items "
                "(id, tenant_id, principal_id, content, content_hash, kind, visibility, "
                "review_status, importance, startup_recall_count, source_type) "
                "VALUES (:id, :tid, :pid, :content, :hash, 'fact', :visibility, "
                "'active', :importance, 9, 'manual')"
            ),
            {
                "id": item_id,
                "tid": tenant["id"],
                "pid": author_id,
                "content": f"feedback integrity {item_id}",
                "hash": f"sha256:{item_id.hex}",
                "importance": importance,
                "visibility": visibility,
            },
        )
    tenant["items"].append(item_id)
    return item_id


async def _key(tenant_id: uuid.UUID, principal_id: uuid.UUID, scopes: list[str]) -> str:
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO api_keys "
                "(id, tenant_id, principal_id, key_id, secret_digest, digest_algorithm, "
                "scopes, label) VALUES (:id, :tid, :pid, :kid, :digest, :algorithm, "
                ":scopes, :label)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "pid": principal_id,
                "kid": parsed.key_id,
                "digest": digest_api_key_secret(parsed.secret),
                "algorithm": DIGEST_ALGORITHM,
                "scopes": scopes,
                "label": f"feedback-key-{uuid.uuid4()}",
            },
        )
    return plaintext


async def _client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    assert _app_factory is not None
    import engram.db as db_module

    settings.auth_enabled = True
    reset_principal_cache()
    monkeypatch.setattr(db_module, "async_session_factory", _app_factory)
    monkeypatch.setattr(db_module, "read_session_factory", _app_factory)
    monkeypatch.setattr(db_module, "owner_session_factory", _owner_factory)
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _service_feedback(
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    principal_type: str,
    item_id: uuid.UUID,
    verdict: str,
    *,
    now: datetime | None = None,
) -> object:
    assert _app_factory is not None
    async with _app_factory() as session:
        await apply_rls_context(session, tenant_id=tenant_id, principal_id=principal_id)
        item = (
            (
                await session.execute(
                    text(
                        "SELECT id, principal_id, importance, startup_recall_count "
                        "FROM memory_items WHERE id = :id FOR UPDATE"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
        return await record_feedback(
            session,
            tenant_id=tenant_id,
            principal_id=principal_id,
            principal_type=principal_type,
            item=item,
            verdict=verdict,  # type: ignore[arg-type]
            recall_log_id=None,
            now=now,
        )


async def test_multiple_api_keys_share_principal_quota_and_admin_does_not_bypass(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_id = await _principal(tenant, "admin")
    author_id = await _principal(tenant, "agent")
    items = [await _item(tenant, author_id) for _ in range(3)]
    key_one = await _key(tenant["id"], admin_id, ["write"])
    key_two = await _key(tenant["id"], admin_id, ["admin"])
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_config SET feedback_daily_limit = 2 WHERE tenant_id = :tid"),
            {"tid": tenant["id"]},
        )
    client = await _client(monkeypatch)
    async with client:
        first = await client.post(
            "/v1/feedback",
            json={"item_id": str(items[0]), "feedback": "useful"},
            headers=_headers(key_one),
        )
        second = await client.post(
            "/v1/feedback",
            json={"item_id": str(items[1]), "feedback": "useful"},
            headers=_headers(key_two),
        )
        limited = await client.post(
            "/v1/feedback",
            json={"item_id": str(items[2]), "feedback": "useful"},
            headers=_headers(key_two),
        )
    assert [first.status_code, second.status_code, limited.status_code] == [201, 201, 429]
    assert int(limited.headers["Retry-After"]) > 0
    assert limited.json()["limit"] == 2


async def test_feedback_utc_day_reset_uses_explicit_now_without_sleep(
    tenant: dict[str, Any],
) -> None:
    user_id = await _principal(tenant, "user")
    author_id = await _principal(tenant, "agent")
    items = [await _item(tenant, author_id) for _ in range(3)]
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_config SET feedback_daily_limit = 1 WHERE tenant_id = :tid"),
            {"tid": tenant["id"]},
        )
    before_midnight = datetime(2026, 7, 11, 23, 59, 58, tzinfo=UTC)
    await _service_feedback(tenant["id"], user_id, "user", items[0], "useful", now=before_midnight)
    with pytest.raises(FeedbackRateLimitError) as exc_info:
        await _service_feedback(
            tenant["id"], user_id, "user", items[1], "useful", now=before_midnight
        )
    assert exc_info.value.reset_at == datetime(2026, 7, 12, tzinfo=UTC)
    after_midnight = datetime(2026, 7, 12, 0, 0, 1, tzinfo=UTC)
    result = await _service_feedback(
        tenant["id"], user_id, "user", items[2], "useful", now=after_midnight
    )
    assert result.status == "recorded"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    (
        "principal_type",
        "is_author",
        "first",
        "second",
        "expected",
        "expected_startup_count",
    ),
    [
        ("user", False, "noise", "useful", 0.55, 0),
        ("admin", False, "useful", "noise", 0.40, 0),
        ("agent", False, "noise", "useful", 0.525, 9),
        ("system", False, "useful", "noise", 0.45, 9),
        ("agent", True, "noise", "useful", 0.50, 9),
        ("system", True, "useful", "noise", 0.50, 9),
    ],
)
async def test_authority_replacements_preserve_canonical_history_and_exact_effect(
    tenant: dict[str, Any],
    principal_type: str,
    is_author: bool,
    first: str,
    second: str,
    expected: float,
    expected_startup_count: int,
) -> None:
    caller_id = await _principal(tenant, principal_type)
    author_id = caller_id if is_author else await _principal(tenant, "agent")
    item_id = await _item(tenant, author_id)
    original = await _service_feedback(tenant["id"], caller_id, principal_type, item_id, first)
    replacement = await _service_feedback(tenant["id"], caller_id, principal_type, item_id, second)
    assert original.status == "recorded"  # type: ignore[attr-defined]
    assert replacement.status == "updated"  # type: ignore[attr-defined]
    assert replacement.previous_feedback == first  # type: ignore[attr-defined]
    async with _owner_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, superseded_at, replaces_feedback_event_id "
                    "FROM feedback_events WHERE tenant_id = :tid AND item_id = :iid "
                    "ORDER BY created_at, id"
                ),
                {"tid": tenant["id"], "iid": item_id},
            )
        ).all()
        state = (
            await session.execute(
                text("SELECT importance, startup_recall_count FROM memory_items WHERE id = :iid"),
                {"iid": item_id},
            )
        ).one()
    assert len(rows) == 2
    assert rows[0].superseded_at is not None
    assert rows[1].replaces_feedback_event_id == rows[0].id
    assert rows[1].superseded_at is None
    assert state.importance == pytest.approx(expected)
    assert state.startup_recall_count == expected_startup_count


async def test_forced_failure_rolls_back_first_and_changed_verdicts(tenant: dict[str, Any]) -> None:
    user_id = await _principal(tenant, "user")
    author_id = await _principal(tenant, "agent")
    first_item = await _item(tenant, author_id)
    changed_item = await _item(tenant, author_id)
    await _service_feedback(tenant["id"], user_id, "user", changed_item, "noise")
    function_name = f"fail_feedback_{uuid.uuid4().hex}"
    trigger_name = f"fail_feedback_{uuid.uuid4().hex}"
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'forced feedback rollback'; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON feedback_events "
                f"FOR EACH ROW WHEN (NEW.item_id IN ('{first_item}', '{changed_item}')) "
                f"EXECUTE FUNCTION {function_name}()"
            )
        )
    try:
        with pytest.raises(Exception, match="forced feedback rollback"):  # noqa: B017
            await _service_feedback(tenant["id"], user_id, "user", first_item, "useful")
        with pytest.raises(Exception, match="forced feedback rollback"):  # noqa: B017
            await _service_feedback(tenant["id"], user_id, "user", changed_item, "useful")
    finally:
        async with _owner_engine.begin() as conn:
            await conn.execute(text(f"DROP TRIGGER {trigger_name} ON feedback_events"))
            await conn.execute(text(f"DROP FUNCTION {function_name}()"))
    async with _owner_factory() as session:
        first_count = await session.scalar(
            text("SELECT count(*) FROM feedback_events WHERE item_id = :iid"),
            {"iid": first_item},
        )
        changed_rows = (
            await session.execute(
                text("SELECT verdict, superseded_at FROM feedback_events WHERE item_id = :iid"),
                {"iid": changed_item},
            )
        ).all()
        states = (
            await session.execute(
                text(
                    "SELECT id, importance, startup_recall_count FROM memory_items "
                    "WHERE id IN (:first_id, :changed_id)"
                ),
                {"first_id": first_item, "changed_id": changed_item},
            )
        ).all()
    assert first_count == 0
    assert [(row.verdict, row.superseded_at) for row in changed_rows] == [("noise", None)]
    state_by_id = {row.id: row for row in states}
    assert state_by_id[first_item].importance == pytest.approx(0.5)
    assert state_by_id[first_item].startup_recall_count == 9
    assert state_by_id[changed_item].importance == pytest.approx(0.4)
    assert state_by_id[changed_item].startup_recall_count == 9


@pytest.mark.parametrize(
    ("initial", "first", "second", "expected"),
    [(0.94, "noise", "useful", 0.95), (0.11, "useful", "noise", 0.10)],
)
async def test_changed_verdict_net_delta_clamps_at_both_bounds(
    tenant: dict[str, Any],
    initial: float,
    first: str,
    second: str,
    expected: float,
) -> None:
    user_id = await _principal(tenant, "user")
    author_id = await _principal(tenant, "agent")
    item_id = await _item(tenant, author_id, importance=initial)
    await _service_feedback(tenant["id"], user_id, "user", item_id, first)
    result = await _service_feedback(tenant["id"], user_id, "user", item_id, second)
    assert result.importance == pytest.approx(expected)  # type: ignore[attr-defined]


async def test_concurrent_distinct_principal_contributions_clamp_without_lost_update(
    tenant: dict[str, Any],
) -> None:
    author_id = await _principal(tenant, "agent")
    item_id = await _item(tenant, author_id, importance=0.94)
    callers = [
        (await _principal(tenant, principal_type), principal_type)
        for principal_type in ("user", "admin", "agent", "system")
    ]
    results = await asyncio.gather(
        *(
            _service_feedback(tenant["id"], principal_id, principal_type, item_id, "useful")
            for principal_id, principal_type in callers
        )
    )
    assert all(result.status == "recorded" for result in results)  # type: ignore[attr-defined]
    async with _owner_factory() as session:
        state = (
            await session.execute(
                text("SELECT importance, startup_recall_count FROM memory_items WHERE id = :iid"),
                {"iid": item_id},
            )
        ).one()
        rows = await session.scalar(
            text(
                "SELECT count(*) FROM feedback_events "
                "WHERE item_id = :iid AND superseded_at IS NULL"
            ),
            {"iid": item_id},
        )
    assert rows == 4
    assert state.importance == pytest.approx(0.95)
    assert state.startup_recall_count == 0


async def test_feedback_scope_and_force_rls_tenant_isolation(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_id = await _principal(tenant, "agent")
    author_id = await _principal(tenant, "agent")
    item_id = await _item(tenant, author_id)
    read_key = await _key(tenant["id"], caller_id, ["read"])
    write_key = await _key(tenant["id"], caller_id, ["write"])
    client = await _client(monkeypatch)
    async with client:
        denied = await client.post(
            "/v1/feedback",
            json={"item_id": str(item_id), "feedback": "useful"},
            headers=_headers(read_key),
        )
    assert denied.status_code == 403

    other: dict[str, Any] = {"id": uuid.uuid4(), "principals": [], "items": []}
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": other["id"], "name": str(other["id"]), "slug": str(other["id"])},
        )
        await conn.execute(
            text("INSERT INTO tenant_config (tenant_id) VALUES (:id)"), {"id": other["id"]}
        )
    try:
        other_principal = await _principal(other, "agent")
        other_author = await _principal(other, "agent")
        other_item = await _item(other, other_author)
        foreign_log = uuid.uuid4()
        async with _owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO recall_logs "
                    "(id, tenant_id, principal_id, mode, item_ids) "
                    "VALUES (:id, :tid, :pid, 'semantic', :items)"
                ),
                {
                    "id": foreign_log,
                    "tid": other["id"],
                    "pid": other_principal,
                    "items": [item_id],
                },
            )
        await _service_feedback(other["id"], other_principal, "agent", other_item, "useful")
        client = await _client(monkeypatch)
        async with client:
            cross_tenant = await client.post(
                "/v1/feedback",
                json={"item_id": str(other_item), "feedback": "useful"},
                headers=_headers(write_key),
            )
            cross_tenant_log = await client.post(
                "/v1/feedback",
                json={
                    "item_id": str(item_id),
                    "feedback": "useful",
                    "recall_log_id": str(foreign_log),
                },
                headers=_headers(write_key),
            )
            local = await client.post(
                "/v1/feedback",
                json={"item_id": str(item_id), "feedback": "useful"},
                headers=_headers(write_key),
            )
        assert cross_tenant.status_code == 404
        assert cross_tenant_log.status_code == 404
        assert local.status_code == 201
        assert _app_factory is not None
        async with _app_factory() as session:
            await apply_rls_context(session, tenant_id=tenant["id"], principal_id=caller_id)
            visible = await session.scalar(text("SELECT count(*) FROM feedback_events"))
            cross_item = await session.scalar(
                text("SELECT count(*) FROM memory_items WHERE id = :iid"),
                {"iid": other_item},
            )
        assert visible == 1
        assert cross_item == 0
        async with _owner_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM feedback_events WHERE tenant_id = :tid"),
                    {"tid": other["id"]},
                )
                == 1
            )
    finally:
        async with _owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": other["id"]})


async def test_rejected_scope_eligibility_and_provenance_do_not_consume_quota(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_id = await _principal(tenant, "agent")
    author_id = await _principal(tenant, "agent")
    private_item = await _item(tenant, author_id, visibility="private")
    eligible_item = await _item(tenant, author_id)
    read_key = await _key(tenant["id"], caller_id, ["read"])
    write_key = await _key(tenant["id"], caller_id, ["write"])
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_config SET feedback_daily_limit = 1 WHERE tenant_id = :tid"),
            {"tid": tenant["id"]},
        )
        foreign_log = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO recall_logs "
                "(id, tenant_id, principal_id, mode, item_ids) "
                "VALUES (:id, :tid, :pid, 'semantic', :items)"
            ),
            {
                "id": foreign_log,
                "tid": tenant["id"],
                "pid": author_id,
                "items": [eligible_item],
            },
        )
    client = await _client(monkeypatch)
    async with client:
        scope_denied = await client.post(
            "/v1/feedback",
            json={"item_id": str(eligible_item), "feedback": "useful"},
            headers=_headers(read_key),
        )
        inaccessible = await client.post(
            "/v1/feedback",
            json={"item_id": str(private_item), "feedback": "useful"},
            headers=_headers(write_key),
        )
        false_provenance = await client.post(
            "/v1/feedback",
            json={
                "item_id": str(eligible_item),
                "feedback": "useful",
                "recall_log_id": str(uuid.uuid4()),
            },
            headers=_headers(write_key),
        )
        another_principal_log = await client.post(
            "/v1/feedback",
            json={
                "item_id": str(eligible_item),
                "feedback": "useful",
                "recall_log_id": str(foreign_log),
            },
            headers=_headers(write_key),
        )
        accepted = await client.post(
            "/v1/feedback",
            json={"item_id": str(eligible_item), "feedback": "useful"},
            headers=_headers(write_key),
        )
    assert [
        scope_denied.status_code,
        inaccessible.status_code,
        false_provenance.status_code,
        another_principal_log.status_code,
        accepted.status_code,
    ] == [403, 404, 404, 404, 201]
    async with _owner_factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM feedback_events WHERE tenant_id = :tid"),
                {"tid": tenant["id"]},
            )
            == 1
        )

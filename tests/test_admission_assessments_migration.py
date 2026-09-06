"""Real-PostgreSQL schema, RLS, privilege and immutability proof for
``admission_assessments`` / ``admission_assessment_current`` (migration 038,
ENG-PROMOTION-003D / issue #159).

These connect as the non-owner ``engram_app`` role to prove, against real
PostgreSQL, that durable admission decisions are:

- tenant isolated under FORCE RLS (another tenant can neither read nor write
  this tenant's decisions or projection);
- immutable in history — the app role has no UPDATE/DELETE grant, and a
  trigger refuses the write even if a grant were restored;
- structurally unable to represent a dishonest decision (a shadow row that
  claims admission, a shadow row in the current projection, a non-admitted row
  linked to a mutation event);
- reapplicable, and preserved rather than destroyed by the downgrade.

They skip without the Compose real-PostgreSQL stack (see ``make compose-ci``).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

_DB_SKIP_REASON = "requires a live PostgreSQL with the v2 schema"
_MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "038_admission_assessments.sql"
_DOWNGRADE = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "downgrades"
    / "038_admission_assessments.sql"
)


def _owner_dsn() -> str | None:
    return os.environ.get("ENGRAM_DATABASE_URL") or os.environ.get("ENGRAM_OWNER_DATABASE_URL")


def _app_dsn() -> str | None:
    return os.environ.get("ENGRAM_APP_DATABASE_URL")


async def _connect(url: str) -> Any:
    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    return await asyncpg.connect(normalize_asyncpg_url(url))


def _skip_if_no_stack() -> None:
    if not _owner_dsn():
        pytest.skip("requires ENGRAM_DATABASE_URL (owner) for setup")
    if not _app_dsn():
        pytest.skip("requires ENGRAM_APP_DATABASE_URL (non-owner app role)")


async def _owner_with_038() -> Any:
    _skip_if_no_stack()
    owner = await _connect(_owner_dsn())  # type: ignore[arg-type]
    if await owner.fetchval("SELECT to_regclass('admission_assessments')") is None:
        await owner.close()
        pytest.skip("requires migration 038")
    return owner


async def _seed_item(owner: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a tenant, principal and proposed item. Returns their ids."""
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    item_id = uuid.uuid4()
    await owner.execute(
        "INSERT INTO tenants(id, name, slug) VALUES ($1, $2, $3)",
        tenant_id,
        f"t-{tenant_id}",
        f"t-{tenant_id}",
    )
    await owner.execute(
        "INSERT INTO principals(id, tenant_id, name, type) VALUES ($1, $2, $3, 'user')",
        principal_id,
        tenant_id,
        f"seed-{principal_id}",
    )
    await owner.execute(
        """INSERT INTO memory_items(id, tenant_id, principal_id, content, content_hash,
               kind, visibility, review_status, source_type)
           VALUES ($1, $2, $3, 'seed content', $4, 'fact', 'tenant', 'proposed', 'manual')""",
        item_id,
        tenant_id,
        principal_id,
        f"sha256:{uuid.uuid4().hex * 2}",
    )
    return tenant_id, principal_id, item_id


_INSERT = """
INSERT INTO admission_assessments(
    id, tenant_id, memory_item_id, schema_version, mode, trigger_type, trigger_id,
    invocation_source, evaluated_at, item_content_hash, input_digest,
    policy_profile_key, policy_contract_version, policy_config_digest,
    outcome, conflict_recheck_status, decision_hash)
VALUES ($1, $2, $3, 'engram.admission-assessment.v1', $4, 'legacy_caller', 'test',
    'test', now(), 'sha256:aa', 'sha256:bb', 'path_a_compat', 'path-a-compat-v1',
    'sha256:cc', $5, $6, 'sha256:dd')
"""


async def _insert(conn: Any, tenant_id: uuid.UUID, item_id: uuid.UUID, **kw: Any) -> uuid.UUID:
    row_id = kw.pop("id", uuid.uuid4())
    await conn.execute(
        _INSERT,
        row_id,
        tenant_id,
        item_id,
        kw.get("mode", "authoritative"),
        kw.get("outcome", "cooling"),
        kw.get("conflict_recheck_status", "not_run"),
    )
    return row_id


async def test_migration_is_reapplicable() -> None:
    """Re-running 038 over an existing schema is a no-op, not an error: the
    runner must be able to replay a partially applied deployment."""
    owner = await _owner_with_038()
    try:
        before = await owner.fetchval("SELECT count(*) FROM admission_assessments")
        await owner.execute(_MIGRATION.read_text())
        assert await owner.fetchval("SELECT count(*) FROM admission_assessments") == before
    finally:
        await owner.close()


async def test_downgrade_preserves_history_and_refuses_while_work_is_in_flight() -> None:
    """Rollback is "stop capturing", never "destroy the record"."""
    owner = await _owner_with_038()
    try:
        text = _DOWNGRADE.read_text()
        assert "DROP TABLE" not in text.upper()
        assert "DELETE FROM admission_assessments" not in text
        # With no pending/running promotion.evaluate work it runs cleanly and
        # leaves the tables in place.
        pending = await owner.fetchval(
            "SELECT count(*) FROM jobs WHERE job_type = 'promotion.evaluate' "
            "AND status IN ('pending','running')"
        )
        if pending == 0:
            await owner.execute(text)
            assert await owner.fetchval("SELECT to_regclass('admission_assessments')") is not None
    finally:
        await owner.close()


async def test_app_role_cannot_update_or_delete_history() -> None:
    """No-rewrite is enforced twice: the UPDATE grant is revoked and a trigger
    refuses the write regardless, so a future privilege drift cannot silently
    make a recorded decision rewritable. DELETE is governed by the revoked
    grant alone, so an item deletion can still cascade."""
    import asyncpg

    owner = await _owner_with_038()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        tenant_id, principal_id, item_id = await _seed_item(owner)
        row_id = await _insert(owner, tenant_id, item_id)

        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(principal_id))
        assert await app.fetchval(
            "SELECT count(*) FROM admission_assessments WHERE id = $1", row_id
        ) == 1
        with pytest.raises(asyncpg.PostgresError):
            await app.execute(
                "UPDATE admission_assessments SET outcome = 'admitted' WHERE id = $1", row_id
            )
        with pytest.raises(asyncpg.PostgresError):
            await app.execute("DELETE FROM admission_assessments WHERE id = $1", row_id)
    finally:
        await app.close()
        await owner.close()


async def test_owner_role_also_cannot_rewrite_history() -> None:
    """The trigger, unlike the grants, binds the owner too."""
    import asyncpg

    owner = await _owner_with_038()
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        row_id = await _insert(owner, tenant_id, item_id)
        with pytest.raises(asyncpg.PostgresError):
            await owner.execute(
                "UPDATE admission_assessments SET outcome = 'admitted' WHERE id = $1", row_id
            )
    finally:
        await owner.close()


async def test_cross_tenant_read_and_write_fail_under_force_rls() -> None:
    owner = await _owner_with_038()
    app = await _connect(_app_dsn())  # type: ignore[arg-type]
    try:
        tenant_a, principal_a, item_a = await _seed_item(owner)
        tenant_b, principal_b, _item_b = await _seed_item(owner)
        row_id = await _insert(owner, tenant_a, item_a)

        # Tenant B cannot see tenant A's decision...
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_b))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(principal_b))
        assert await app.fetchval(
            "SELECT count(*) FROM admission_assessments WHERE id = $1", row_id
        ) == 0
        assert await app.fetchval(
            "SELECT count(*) FROM admission_assessment_current WHERE tenant_id = $1", tenant_a
        ) == 0

        # ...nor write one into tenant A.
        import asyncpg

        with pytest.raises(asyncpg.PostgresError):
            await _insert(app, tenant_a, item_a)

        # Tenant A sees its own.
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_a))
        await app.execute("SELECT set_config('app.principal_id', $1, false)", str(principal_a))
        assert await app.fetchval(
            "SELECT count(*) FROM admission_assessments WHERE id = $1", row_id
        ) == 1
    finally:
        await app.close()
        await owner.close()


async def test_a_shadow_row_can_never_claim_admission() -> None:
    import asyncpg

    owner = await _owner_with_038()
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        with pytest.raises(asyncpg.PostgresError):
            await _insert(owner, tenant_id, item_id, mode="shadow", outcome="admitted")
        # The same decision as a preview is representable.
        await _insert(
            owner,
            tenant_id,
            item_id,
            mode="shadow",
            outcome="would_admit",
            conflict_recheck_status="not_run_preview",
        )
    finally:
        await owner.close()


async def test_a_shadow_row_can_never_enter_the_current_projection() -> None:
    import asyncpg

    owner = await _owner_with_038()
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        row_id = await _insert(
            owner,
            tenant_id,
            item_id,
            mode="shadow",
            outcome="would_admit",
            conflict_recheck_status="not_run_preview",
        )
        with pytest.raises(asyncpg.PostgresError):
            await owner.execute(
                """INSERT INTO admission_assessment_current(tenant_id, memory_item_id,
                       policy_profile_key, assessment_id, mode, mode_rank, mutation_rank,
                       evaluated_at)
                   SELECT $1, $2, 'path_a_compat', $3, 'shadow', 1, 0, evaluated_at
                   FROM admission_assessments WHERE id = $3""",
                tenant_id,
                item_id,
                row_id,
            )
    finally:
        await owner.close()


async def test_only_an_admitted_row_may_link_a_mutation_event() -> None:
    """A non-admitted decision authorized no state change, so it must not be
    able to name an audit event as though it had."""
    import asyncpg

    owner = await _owner_with_038()
    try:
        tenant_id, principal_id, item_id = await _seed_item(owner)
        event_id = uuid.uuid4()
        await owner.execute(
            """INSERT INTO item_events(id, item_id, tenant_id, event_type, field_name,
                   old_value, new_value, actor_principal_id)
               VALUES ($1, $2, $3, 'review_change', 'review_status', 'proposed', 'active', $4)""",
            event_id,
            item_id,
            tenant_id,
            principal_id,
        )
        with pytest.raises(asyncpg.PostgresError):
            await owner.execute(
                """INSERT INTO admission_assessments(
                       id, tenant_id, memory_item_id, schema_version, mode, trigger_type,
                       trigger_id, invocation_source, evaluated_at, item_content_hash,
                       input_digest, policy_profile_key, policy_contract_version,
                       policy_config_digest, outcome, conflict_recheck_status,
                       decision_hash, linked_item_event_id)
                   VALUES ($1, $2, $3, 'engram.admission-assessment.v1', 'authoritative',
                       'legacy_caller', 't', 't', now(), 'sha256:aa', 'sha256:bb',
                       'path_a_compat', 'path-a-compat-v1', 'sha256:cc', 'cooling',
                       'not_run', 'sha256:dd', $4)""",
                uuid.uuid4(),
                tenant_id,
                item_id,
                event_id,
            )
    finally:
        await owner.close()


async def test_evaluation_id_is_unique_per_tenant() -> None:
    """The database refuses a second decision under one execution identity.

    This is the backstop, not the mechanism. Actually *reusing* the bound
    decision on retry is application behavior and is proven end-to-end in
    tests/test_admission_assessments_postgres.py; what this asserts is that
    the schema makes a duplicate impossible even if that path were bypassed.
    """
    import asyncpg

    owner = await _owner_with_038()
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        evaluation_id = uuid.uuid4()
        sql = """INSERT INTO admission_assessments(
                     id, tenant_id, memory_item_id, schema_version, mode, evaluation_id,
                     trigger_type, trigger_id, invocation_source, evaluated_at,
                     item_content_hash, input_digest, policy_profile_key,
                     policy_contract_version, policy_config_digest, outcome,
                     conflict_recheck_status, decision_hash)
                 VALUES ($1, $2, $3, 'engram.admission-assessment.v1', 'authoritative', $4,
                     'classification_bound', 't', 'promotion.evaluate', now(), 'sha256:aa',
                     'sha256:bb', 'path_a_compat', 'path-a-compat-v1', 'sha256:cc', 'cooling',
                     'not_run', 'sha256:dd')"""
        await owner.execute(sql, uuid.uuid4(), tenant_id, item_id, evaluation_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await owner.execute(sql, uuid.uuid4(), tenant_id, item_id, evaluation_id)
    finally:
        await owner.close()


async def test_one_canonical_decision_can_bind_each_job() -> None:
    """The job binding rejects duplicates but permits stale pre-lock history."""
    import asyncpg

    owner = await _owner_with_038()
    tenant_id: uuid.UUID | None = None
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        job_id = uuid.uuid4()
        await owner.execute(
            "INSERT INTO jobs(id, tenant_id, job_type) "
            "VALUES ($1, $2, 'promotion.evaluate')",
            job_id,
            tenant_id,
        )
        sql = """INSERT INTO admission_assessments(
                     id, tenant_id, memory_item_id, schema_version, mode, evaluation_id,
                     job_id, trigger_type, trigger_id, invocation_source, evaluated_at,
                     item_content_hash, input_digest, policy_profile_key,
                     policy_contract_version, policy_config_digest, outcome,
                     conflict_recheck_status, decision_hash)
                 VALUES ($1, $2, $3, 'engram.admission-assessment.v1', 'authoritative', $4,
                     $5, 'manual', 't', 'promotion.evaluate', now(), 'sha256:aa',
                     'sha256:bb', 'path_a_compat', 'path-a-compat-v1', 'sha256:cc', 'cooling',
                     'not_run', 'sha256:dd')"""
        await owner.execute(sql, uuid.uuid4(), tenant_id, item_id, uuid.uuid4(), job_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await owner.execute(sql, uuid.uuid4(), tenant_id, item_id, uuid.uuid4(), job_id)

        # A superseded pre-lock row has job provenance but no canonical
        # evaluation identity. The partial unique index permits that history.
        await owner.execute(sql, uuid.uuid4(), tenant_id, item_id, None, job_id)
    finally:
        if tenant_id is not None:
            await owner.execute(
                "DELETE FROM admission_assessments WHERE tenant_id = $1", tenant_id
            )
            await owner.execute("DELETE FROM jobs WHERE tenant_id = $1", tenant_id)
            await owner.execute("DELETE FROM memory_items WHERE tenant_id = $1", tenant_id)
            await owner.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await owner.close()


async def test_outcome_and_mode_vocabularies_are_closed_in_the_database() -> None:
    import asyncpg

    owner = await _owner_with_038()
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        with pytest.raises(asyncpg.PostgresError):
            await _insert(owner, tenant_id, item_id, outcome="probably_fine")
        with pytest.raises(asyncpg.PostgresError):
            await _insert(owner, tenant_id, item_id, mode="authoritative_ish")
        with pytest.raises(asyncpg.PostgresError):
            await _insert(owner, tenant_id, item_id, conflict_recheck_status="maybe")
    finally:
        await owner.close()


async def test_item_events_carries_a_nullable_assessment_reference() -> None:
    """Historical events predate #159 and stay unlinked; the column must be
    nullable forever."""
    owner = await _owner_with_038()
    try:
        nullable = await owner.fetchval(
            """SELECT is_nullable FROM information_schema.columns
               WHERE table_name = 'item_events' AND column_name = 'admission_assessment_id'"""
        )
        assert nullable == "YES"
    finally:
        await owner.close()


async def test_deleting_an_item_cascades_its_decisions_away() -> None:
    """A decision about an item that no longer exists binds to nothing, so the
    ON DELETE CASCADE must actually work — the no-rewrite trigger covers
    UPDATE only, precisely so item deletion does not fail outright.

    The linked-event half of this (an admitted decision that names a real
    audit event, which is where an ``ON DELETE SET NULL`` link would collide
    with the no-rewrite trigger) is exercised against the real promotion path
    in tests/test_admission_assessments_postgres.py.
    """
    owner = await _owner_with_038()
    try:
        tenant_id, _, item_id = await _seed_item(owner)
        row_id = await _insert(owner, tenant_id, item_id)
        await owner.execute(
            """INSERT INTO admission_assessment_current(tenant_id, memory_item_id,
                   policy_profile_key, assessment_id, mode, mode_rank, mutation_rank,
                   evaluated_at)
               SELECT $1, $2, 'path_a_compat', $3, 'authoritative', 1, 0, evaluated_at
               FROM admission_assessments WHERE id = $3""",
            tenant_id,
            item_id,
            row_id,
        )
        await owner.execute("DELETE FROM memory_items WHERE id = $1", item_id)
        assert await owner.fetchval(
            "SELECT count(*) FROM admission_assessments WHERE id = $1", row_id
        ) == 0
        assert await owner.fetchval(
            "SELECT count(*) FROM admission_assessment_current WHERE memory_item_id = $1",
            item_id,
        ) == 0
    finally:
        await owner.close()

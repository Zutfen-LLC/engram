"""DB-independent parity + exclusion coverage for engram.memory_access.

ENG-SCOPE-001: after migration 021, ``visibility='workspace' AND
workspace_id IS NULL`` is unrepresentable in real Postgres (CHECK
constraint). This file proves the *predicate* itself — both the SQLAlchemy
expression and the raw-SQL fragment — correctly excludes that shape even if
one is manually constructed (e.g. a stray fixture row), and that the two
forms agree across every visibility category. Runs against SQLite (no CHECK
constraint mirrored there), so it always runs — never skips for lack of a
live Postgres.

IDs are kept as real ``uuid.UUID`` objects end to end and stored/bound as
``.hex`` (32 lowercase hex chars, no hyphens): ``MemoryItem``'s
``tenant_id``/``workspace_id``/``principal_id`` columns use
``postgresql.UUID(as_uuid=True)``, whose bind processor on a non-native-UUID
dialect (SQLite has none) converts every bound ``UUID`` to ``value.hex`` —
so raw ``text()`` inserts/params, which bind through the SQLite DBAPI with no
such processor, must use the identical ``.hex`` form or the two code paths
silently compare against differently-formatted strings and never match.
``uuid.UUID(hex_string)`` parses either form back losslessly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engram.memory_access import (
    eligibility_expression,
    eligibility_sql,
    read_eligibility_expression,
    read_eligibility_sql,
    tenant_sql,
)
from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext
from engram.models import MemoryItem

pytestmark = pytest.mark.asyncio

_CREATE = [
    """
    CREATE TABLE memory_items (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        workspace_id TEXT,
        principal_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        kind TEXT NOT NULL,
        visibility TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workspace_members (
        workspace_id TEXT NOT NULL,
        principal_id TEXT NOT NULL
    )
    """,
]


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for stmt in _CREATE:
            await conn.exec_driver_sql(stmt)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _insert_item(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    visibility: str,
    workspace_id: uuid.UUID | None,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO memory_items "
            "(id, tenant_id, workspace_id, principal_id, content, content_hash, kind, visibility) "
            "VALUES (:id, :tid, :wid, :pid, 'x', :hash, 'fact', :vis)"
        ),
        {
            "id": item_id.hex,
            "tid": tenant_id.hex,
            "wid": workspace_id.hex if workspace_id is not None else None,
            "pid": principal_id.hex,
            "hash": f"hash-{item_id}",
            "vis": visibility,
        },
    )
    await session.commit()
    return item_id


async def _orm_eligible_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = (
        (
            await session.execute(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == tenant_id,
                    eligibility_expression(principal_id),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def _raw_eligible_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> set[uuid.UUID]:
    stmt = text(f"SELECT id FROM memory_items WHERE {tenant_sql()} AND {eligibility_sql()}")
    rows = (
        (
            await session.execute(
                stmt,
                {"caller_tenant_id": tenant_id.hex, "caller_principal_id": principal_id.hex},
            )
        )
        .scalars()
        .all()
    )
    return {uuid.UUID(r) for r in rows}


def _context(
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    *,
    private: bool,
    tenant: bool,
    public: bool,
    workspaces: frozenset[uuid.UUID],
) -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=tenant_id,
        principal_id=principal_id,
        api_key_id=uuid.uuid4(),
        memory_profile_id=uuid.uuid4(),
        memory_profile_revision_id=uuid.uuid4(),
        memory_profile_slug="unit-profile",
        memory_profile_version=1,
        include_private=private,
        include_tenant=tenant,
        include_public=public,
        readable_workspace_ids=workspaces,
    )


async def _context_ids(
    session: AsyncSession, context: ResolvedMemoryContext
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    orm = set(
        (
            await session.execute(
                select(MemoryItem.id).where(read_eligibility_expression(context))
            )
        )
        .scalars()
        .all()
    )
    predicate = read_eligibility_sql(context, parameter_prefix="unit_item")
    sqlite_params = {
        key: value.replace("-", "") if isinstance(value, str) else value
        for key, value in predicate.params.items()
    }
    raw_rows = (
        await session.execute(
            text(f"SELECT id FROM memory_items WHERE {predicate.clause}"), sqlite_params
        )
    ).scalars().all()
    return orm, {uuid.UUID(value) for value in raw_rows}


async def test_manually_constructed_workspace_null_row_excluded_by_both_forms(session):
    tenant_id, owner_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # The impossible-after-migration shape: visibility='workspace', no workspace.
    await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=owner_id,
        visibility="workspace",
        workspace_id=None,
    )

    for principal_id in (owner_id, other_id):
        assert (
            await _orm_eligible_ids(session, tenant_id=tenant_id, principal_id=principal_id)
        ) == set()
        assert (
            await _raw_eligible_ids(session, tenant_id=tenant_id, principal_id=principal_id)
        ) == set()


async def test_orm_and_raw_sql_predicates_agree_across_visibility_categories(session):
    tenant_id = uuid.uuid4()
    owner_id, member_id, nonmember_id, other_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    workspace_id = uuid.uuid4()
    # owner_id writes the workspace item as an actual member (realistic: the
    # write-scope resolver requires membership to write workspace-visibility
    # in the first place) — membership is what grants read access, not
    # authorship, so member_id needs it too.
    for pid in (owner_id, member_id):
        await session.execute(
            text("INSERT INTO workspace_members (workspace_id, principal_id) VALUES (:wid, :pid)"),
            {"wid": workspace_id.hex, "pid": pid.hex},
        )
    await session.commit()

    tenant_item = await _insert_item(
        session, tenant_id=tenant_id, principal_id=owner_id, visibility="tenant", workspace_id=None
    )
    public_item = await _insert_item(
        session, tenant_id=tenant_id, principal_id=owner_id, visibility="public", workspace_id=None
    )
    private_owned = await _insert_item(
        session, tenant_id=tenant_id, principal_id=owner_id, visibility="private", workspace_id=None
    )
    private_other = await _insert_item(
        session, tenant_id=tenant_id, principal_id=other_id, visibility="private", workspace_id=None
    )
    workspace_item = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=owner_id,
        visibility="workspace",
        workspace_id=workspace_id,
    )
    # The impossible-after-migration shape — must never be admitted.
    await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=owner_id,
        visibility="workspace",
        workspace_id=None,
    )

    for principal_id, expected in (
        (owner_id, {tenant_item, public_item, private_owned, workspace_item}),
        (member_id, {tenant_item, public_item, workspace_item}),
        (nonmember_id, {tenant_item, public_item}),
        (other_id, {tenant_item, public_item, private_other}),
    ):
        orm_ids = await _orm_eligible_ids(session, tenant_id=tenant_id, principal_id=principal_id)
        raw_ids = await _raw_eligible_ids(session, tenant_id=tenant_id, principal_id=principal_id)
        assert orm_ids == raw_ids == expected, principal_id


async def test_profile_predicate_truth_table_and_raw_orm_parity(session):
    tenant_id = uuid.uuid4()
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO workspace_members (workspace_id, principal_id) VALUES (:wid, :pid)"),
        {"wid": workspace_a.hex, "pid": owner_id.hex},
    )
    await session.commit()

    private_null = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=owner_id,
        visibility="private",
        workspace_id=None,
    )
    private_a = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=owner_id,
        visibility="private",
        workspace_id=workspace_a,
    )
    await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=other_id,
        visibility="private",
        workspace_id=None,
    )
    tenant_null = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=other_id,
        visibility="tenant",
        workspace_id=None,
    )
    tenant_a = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=other_id,
        visibility="tenant",
        workspace_id=workspace_a,
    )
    await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=other_id,
        visibility="tenant",
        workspace_id=workspace_b,
    )
    public_null = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=other_id,
        visibility="public",
        workspace_id=None,
    )
    workspace_item_a = await _insert_item(
        session,
        tenant_id=tenant_id,
        principal_id=other_id,
        visibility="workspace",
        workspace_id=workspace_a,
    )

    cases = (
        (_context(tenant_id, owner_id, private=True, tenant=False, public=False,
                  workspaces=frozenset()), {private_null}),
        (_context(tenant_id, owner_id, private=False, tenant=True, public=False,
                  workspaces=frozenset()), {tenant_null}),
        (_context(tenant_id, owner_id, private=False, tenant=False, public=True,
                  workspaces=frozenset()), {public_null}),
        (_context(tenant_id, owner_id, private=True, tenant=True, public=True,
                  workspaces=frozenset({workspace_a})),
         {private_null, private_a, tenant_null, tenant_a, public_null, workspace_item_a}),
        (_context(tenant_id, owner_id, private=False, tenant=False, public=False,
                  workspaces=frozenset({workspace_a})), {workspace_item_a}),
        (_context(tenant_id, owner_id, private=False, tenant=False, public=False,
                  workspaces=frozenset()), set()),
    )
    for context, expected in cases:
        orm, raw = await _context_ids(session, context)
        assert orm == raw == expected


async def test_raw_predicates_support_independent_alias_prefixes() -> None:
    tenant_id, principal_id, workspace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    context = _context(
        tenant_id,
        principal_id,
        private=True,
        tenant=True,
        public=True,
        workspaces=frozenset({workspace_id}),
    )
    left = read_eligibility_sql(context, alias="left_item", parameter_prefix="left")
    right = read_eligibility_sql(context, alias="right_item", parameter_prefix="right")
    assert set(left.params).isdisjoint(right.params)
    assert "left_item." in left.clause
    assert "right_item." in right.clause
    assert str(workspace_id) not in left.clause + right.clause

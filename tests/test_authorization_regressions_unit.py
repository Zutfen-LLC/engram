from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from engram.memory_access import write_eligibility_expression
from engram.memory_context import ResolvedMemoryContext
from engram.models import MemoryItem


def _context() -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        version="memory-context-v2",
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        memory_profile_id=uuid.uuid4(),
        memory_profile_revision_id=uuid.uuid4(),
        memory_profile_slug="private-admin",
        memory_profile_version=1,
        include_private=True,
        include_tenant=False,
        include_public=False,
        readable_workspace_ids=frozenset(),
        allow_tenant_write=False,
        allow_public_write=False,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=frozenset(),
        admin_workspace_bypass=True,
    )


def test_complete_promotion_predicate_keeps_private_ownership_requirement() -> None:
    context = _context()
    statement = select(MemoryItem).where(write_eligibility_expression(context))
    compiled = statement.compile(
        dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "memory_items.principal_id" in sql
    assert str(context.principal_id) in sql
    assert "memory_items.visibility = 'private'" in sql

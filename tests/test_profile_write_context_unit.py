from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from engram.memory_context import MEMORY_CONTEXT_VERSION, ResolvedMemoryContext
from engram.memory_scope import assert_write_scope_allowed


def _context(
    *,
    include_private: bool = True,
    include_tenant: bool = True,
    include_public: bool = True,
    allow_tenant_write: bool = False,
    allow_public_write: bool = False,
    readable: frozenset[UUID] = frozenset(),
    writable: frozenset[UUID] = frozenset(),
) -> ResolvedMemoryContext:
    return ResolvedMemoryContext(
        version=MEMORY_CONTEXT_VERSION,
        tenant_id=uuid4(),
        principal_id=uuid4(),
        api_key_id=uuid4(),
        memory_profile_id=uuid4(),
        memory_profile_revision_id=uuid4(),
        memory_profile_slug="write-policy",
        memory_profile_version=1,
        include_private=include_private,
        include_tenant=include_tenant,
        include_public=include_public,
        readable_workspace_ids=readable,
        allow_tenant_write=allow_tenant_write,
        allow_public_write=allow_public_write,
        default_write_visibility="private",
        default_write_workspace_id=None,
        writable_workspace_ids=writable,
    )


def test_private_creation_is_independent_of_include_private() -> None:
    context = _context(include_private=False)
    assert context.allows_new_write_scope("private", None)
    assert not context.allows_existing_item_scope("private", None)


def test_tenant_and_public_creation_flags_are_independent_of_read_flags() -> None:
    context = _context(
        include_tenant=False,
        include_public=False,
        allow_tenant_write=True,
        allow_public_write=True,
    )
    assert context.allows_new_write_scope("tenant", None)
    assert context.allows_new_write_scope("public", None)
    assert not context.allows_existing_item_scope("tenant", None)
    assert not context.allows_existing_item_scope("public", None)


def test_every_workspace_association_requires_a_write_grant() -> None:
    workspace_a, workspace_b = uuid4(), uuid4()
    context = _context(
        allow_tenant_write=True,
        allow_public_write=True,
        readable=frozenset({workspace_a, workspace_b}),
        writable=frozenset({workspace_a}),
    )
    for visibility in ("private", "workspace", "tenant", "public"):
        assert context.allows_new_write_scope(visibility, workspace_a)
        assert not context.allows_new_write_scope(visibility, workspace_b)


def test_existing_item_requires_read_and_write_intersection() -> None:
    workspace_a, workspace_b = uuid4(), uuid4()
    context = _context(
        allow_tenant_write=False,
        allow_public_write=True,
        readable=frozenset({workspace_a, workspace_b}),
        writable=frozenset({workspace_a}),
    )
    assert context.allows_existing_item_scope("workspace", workspace_a)
    assert not context.allows_existing_item_scope("workspace", workspace_b)
    assert not context.allows_existing_item_scope("tenant", None)
    assert context.allows_existing_item_scope("public", None)


def test_profile_denials_keep_workspace_non_disclosing() -> None:
    workspace_id = uuid4()
    context = _context(readable=frozenset({workspace_id}), writable=frozenset())
    with pytest.raises(HTTPException) as exc_info:
        assert_write_scope_allowed(
            context, visibility="private", workspace_id=workspace_id
        )
    assert exc_info.value.status_code == 404


def test_category_denial_is_bounded_403() -> None:
    context = _context(allow_tenant_write=False)
    with pytest.raises(HTTPException) as exc_info:
        assert_write_scope_allowed(context, visibility="tenant", workspace_id=None)
    assert exc_info.value.status_code == 403

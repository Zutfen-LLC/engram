from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from engram.auth import Principal
from engram.config import settings
from engram.memory_context import (
    MEMORY_CONTEXT_VERSION,
    ResolvedMemoryContext,
    resolve_memory_context,
    unrestricted_memory_context,
)


def _bound_context(**overrides: object) -> ResolvedMemoryContext:
    values: dict[str, object] = {
        "version": MEMORY_CONTEXT_VERSION,
        "tenant_id": uuid4(),
        "principal_id": uuid4(),
        "api_key_id": uuid4(),
        "memory_profile_id": uuid4(),
        "memory_profile_revision_id": uuid4(),
        "memory_profile_slug": "unit",
        "memory_profile_version": 1,
        "include_private": False,
        "include_tenant": False,
        "include_public": False,
        "readable_workspace_ids": frozenset(),
        "allow_tenant_write": False,
        "allow_public_write": False,
        "default_write_visibility": "private",
        "default_write_workspace_id": None,
        "writable_workspace_ids": frozenset(),
    }
    values.update(overrides)
    return ResolvedMemoryContext(**values)  # type: ignore[arg-type]


def test_context_properties_distinguish_unprofiled_empty_and_workspace_only() -> None:
    principal = Principal(
        tenant_id=str(uuid4()), principal_id=str(uuid4()), scopes=("read",)
    )
    unprofiled = unrestricted_memory_context(principal)
    assert unprofiled.version == MEMORY_CONTEXT_VERSION
    assert not unprofiled.is_profile_bound
    assert unprofiled.may_read_anything
    assert unprofiled.allows_workspace(uuid4())

    empty = _bound_context()
    assert empty.is_profile_bound
    assert not empty.may_read_anything
    assert not empty.allows_workspace(uuid4())
    assert empty.allows_workspace(None)

    workspace_id = uuid4()
    workspace_only = _bound_context(readable_workspace_ids=frozenset({workspace_id}))
    assert workspace_only.may_read_anything
    assert workspace_only.allows_workspace(workspace_id)
    assert not workspace_only.allows_workspace(uuid4())


class _MappingsResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def mappings(self) -> _MappingsResult:
        return self

    def first(self) -> dict[str, object] | None:
        return self.row


class _Session:
    bind = None

    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.query_count = 0

    async def execute(self, *_args: object, **_kwargs: object) -> _MappingsResult:
        self.query_count += 1
        return _MappingsResult(self.row)


async def test_profile_resolution_loads_revision_and_grants_once(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    tenant_id, principal_id = uuid4(), uuid4()
    profile_id, revision_id, api_key_id = uuid4(), uuid4(), uuid4()
    workspace_a, workspace_b = uuid4(), uuid4()
    principal = Principal(
        tenant_id=str(tenant_id),
        principal_id=str(principal_id),
        scopes=("read",),
        api_key_id=str(api_key_id),
        memory_profile_id=str(profile_id),
        memory_profile_revision_id=str(revision_id),
        memory_profile_slug="pinned",
        memory_profile_version=3,
    )
    session = _Session(
        {
            "profile_id": profile_id,
            "slug": "pinned",
            "disabled_at": None,
            "revision_id": revision_id,
            "version": 3,
            "include_private": True,
            "include_tenant": False,
            "include_public": True,
            "allow_tenant_write": False,
            "allow_public_write": True,
            "default_write_visibility": "private",
            "default_write_workspace_id": None,
            "readable_workspace_ids": [workspace_a, workspace_b],
            "writable_workspace_ids": [workspace_a],
        }
    )
    context = await resolve_memory_context(session, principal)  # type: ignore[arg-type]
    assert session.query_count == 1
    assert context.memory_profile_revision_id == revision_id
    assert context.readable_workspace_ids == frozenset({workspace_a, workspace_b})
    assert context.writable_workspace_ids == frozenset({workspace_a})


@pytest.mark.parametrize("row", [None, {"disabled_at": datetime.now(UTC)}])
async def test_incoherent_profile_resolution_is_generic_401(monkeypatch, row) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    principal = Principal(
        tenant_id=str(uuid4()),
        principal_id=str(uuid4()),
        scopes=("read",),
        api_key_id=str(uuid4()),
        memory_profile_id=str(uuid4()),
        memory_profile_revision_id=str(uuid4()),
        memory_profile_slug="missing",
        memory_profile_version=1,
    )
    with pytest.raises(HTTPException) as exc_info:
        await resolve_memory_context(_Session(row), principal)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or revoked API key"

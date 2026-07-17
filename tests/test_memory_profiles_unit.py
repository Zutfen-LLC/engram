"""Database-free policy validation for memory profiles."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from engram.memory_profiles import (
    ProfilePolicyInput,
    ProfileValidationError,
    WorkspaceGrantInput,
    validate_policy,
    validate_slug,
)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


def test_slug_validation_accepts_stable_slug_and_rejects_invalid_shapes() -> None:
    assert validate_slug("coding-agent-2") == "coding-agent-2"
    for value in ("Coding", "coding_agent", "-coding", "coding-", "coding--agent", ""):
        with pytest.raises(ProfileValidationError, match="slug"):
            validate_slug(value)


async def test_private_only_policy_is_the_safe_default() -> None:
    session = AsyncMock()
    grants = await validate_policy(session, uuid4(), ProfilePolicyInput())
    assert grants == []
    session.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            ProfilePolicyInput(
                default_write_visibility="private", default_write_workspace_id=uuid4()
            ),
            "private default write",
        ),
        (ProfilePolicyInput(default_write_visibility="workspace"), "requires a workspace"),
        (ProfilePolicyInput(default_write_visibility="tenant"), "allow_tenant_write"),
        (ProfilePolicyInput(default_write_visibility="public"), "allow_public_write"),
    ],
)
async def test_default_write_shape_rules(policy: ProfilePolicyInput, message: str) -> None:
    with pytest.raises(ProfileValidationError, match=message):
        await validate_policy(AsyncMock(), uuid4(), policy)


@pytest.mark.parametrize(
    ("grants", "message"),
    [
        (
            lambda workspace: (
                WorkspaceGrantInput(workspace, True, False),
                WorkspaceGrantInput(workspace, True, False),
            ),
            "unique",
        ),
        (
            lambda workspace: (WorkspaceGrantInput(workspace, False, True),),
            "can_write requires can_read",
        ),
        (
            lambda workspace: (WorkspaceGrantInput(workspace, False, False),),
            "allow read or write",
        ),
    ],
)
async def test_invalid_grant_shapes_fail_before_workspace_lookup(grants: Any, message: str) -> None:
    workspace = uuid4()
    session = AsyncMock()
    with pytest.raises(ProfileValidationError, match=message):
        await validate_policy(
            session, uuid4(), ProfilePolicyInput(workspace_grants=grants(workspace))
        )
    session.execute.assert_not_awaited()


async def test_workspace_default_requires_tenant_local_writable_grant() -> None:
    tenant = uuid4()
    workspace = uuid4()
    session = AsyncMock()
    session.execute.return_value = _Rows([{"id": workspace, "slug": "engineering"}])

    resolved = await validate_policy(
        session,
        tenant,
        ProfilePolicyInput(
            default_write_visibility="workspace",
            default_write_workspace_id=workspace,
            workspace_grants=(WorkspaceGrantInput(workspace, True, True),),
        ),
    )
    assert resolved[0].workspace_slug == "engineering"

    session.execute.return_value = _Rows([])
    with pytest.raises(ProfileValidationError, match="not found in this tenant"):
        await validate_policy(
            session,
            tenant,
            ProfilePolicyInput(workspace_grants=(WorkspaceGrantInput(workspace, True, False),)),
        )

    session.execute.return_value = _Rows([{"id": workspace, "slug": "engineering"}])
    with pytest.raises(ProfileValidationError, match="writable workspace grant"):
        await validate_policy(
            session,
            tenant,
            ProfilePolicyInput(
                default_write_visibility="workspace",
                default_write_workspace_id=workspace,
                workspace_grants=(WorkspaceGrantInput(workspace, True, False),),
            ),
        )

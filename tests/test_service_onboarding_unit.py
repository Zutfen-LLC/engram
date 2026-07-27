"""Pure contracts for service-authenticated workspace/agent onboarding."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from engram.api.routes.service_provisioning import (
    ServiceAgentKeyRequest,
    ServiceWorkspaceAgentRequest,
)
from engram.service_auth import canonicalize_service_permissions
from engram.service_onboarding import (
    AgentApiKeyInput,
    AgentInput,
    AgentKeyRequest,
    WorkspaceAgentRequest,
    WorkspaceInput,
    agent_key_request_digest,
    workspace_agent_request_digest,
)


def _digest(payload: dict[str, object]) -> bytes:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).digest()


def test_all_service_permissions_canonicalize_in_locked_order() -> None:
    assert canonicalize_service_permissions(
        [
            "api_key.provision",
            "workspace.provision",
            "tenant.provision",
            "agent.provision",
            "principal.provision",
        ]
    ) == [
        "tenant.provision",
        "principal.provision",
        "workspace.provision",
        "agent.provision",
        "api_key.provision",
    ]


def test_workspace_agent_request_digest_is_explicit_and_canonical() -> None:
    request = WorkspaceAgentRequest(
        tenant_external_ref="tenant-ref",
        workspace=WorkspaceInput(
            external_ref="workspace-ref", name="Workspace", slug="workspace"
        ),
        agent=AgentInput(external_ref="agent-ref", name="Agent"),
    )
    assert workspace_agent_request_digest(request) == _digest(
        {
            "v": 1,
            "tenant_external_ref": "tenant-ref",
            "workspace": {
                "external_ref": "workspace-ref",
                "name": "Workspace",
                "slug": "workspace",
            },
            "agent": {
                "external_ref": "agent-ref",
                "name": "Agent",
                "type": "agent",
            },
            "membership": {"role": "member"},
        }
    )


def test_agent_key_request_digest_includes_nulls_and_fixed_policy() -> None:
    request = AgentKeyRequest(
        tenant_external_ref="tenant-ref",
        workspace_external_ref="workspace-ref",
        agent_external_ref="agent-ref",
        api_key=AgentApiKeyInput(
            external_ref="key-ref", label=None, replaces_external_ref=None
        ),
    )
    assert agent_key_request_digest(request) == _digest(
        {
            "v": 1,
            "tenant_external_ref": "tenant-ref",
            "workspace_external_ref": "workspace-ref",
            "agent_external_ref": "agent-ref",
            "api_key": {
                "external_ref": "key-ref",
                "label": None,
                "replaces_external_ref": None,
            },
            "scopes": ["read", "write"],
            "memory_profile_id": None,
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "tenant_external_ref": "tenant-ref",
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "workspace": {
                "external_ref": "workspace-ref",
                "name": "Workspace",
                "slug": "workspace",
            },
            "agent": {"external_ref": "agent-ref", "name": "Agent"},
        },
        {
            "tenant_external_ref": "tenant-ref",
            "workspace": {
                "external_ref": "workspace-ref",
                "name": "Workspace",
                "slug": "Workspace",
            },
            "agent": {"external_ref": "agent-ref", "name": "Agent"},
        },
        {
            "tenant_external_ref": "tenant-ref",
            "workspace": {
                "external_ref": "workspace-ref",
                "name": "Workspace",
                "slug": "workspace",
            },
            "agent": {
                "external_ref": "agent-ref",
                "name": "__engram_internal_review__spoof",
            },
        },
    ],
)
def test_workspace_agent_schema_rejects_forbidden_or_invalid_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ServiceWorkspaceAgentRequest.model_validate(payload)


def test_agent_key_schema_rejects_authority_fields_and_self_replacement() -> None:
    base = {
        "tenant_external_ref": "tenant-ref",
        "workspace_external_ref": "workspace-ref",
        "agent_external_ref": "agent-ref",
        "api_key": {
            "external_ref": "key-ref",
            "label": "agent key",
            "replaces_external_ref": "key-ref",
        },
    }
    with pytest.raises(ValidationError):
        ServiceAgentKeyRequest.model_validate(base)
    base["api_key"] = {
        "external_ref": "key-ref",
        "label": "agent key",
        "scopes": ["admin"],
    }
    with pytest.raises(ValidationError):
        ServiceAgentKeyRequest.model_validate(base)

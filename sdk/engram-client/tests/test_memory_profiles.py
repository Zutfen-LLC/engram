"""SDK contract tests for memory-profile control-plane methods."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from engram_client import (
    AgentCreateRequest,
    ApiKeyCreateRequest,
    EngramClient,
    EngramNotFoundError,
    MemoryProfileCreate,
    MemoryProfilePolicy,
    MemoryProfileRevisionCreate,
    WorkspaceGrantInput,
)

PROFILE_ID = "11111111-1111-1111-1111-111111111111"
REVISION_ID = "22222222-2222-2222-2222-222222222222"
WORKSPACE_ID = "33333333-3333-3333-3333-333333333333"
TENANT_ID = "44444444-4444-4444-4444-444444444444"
PRINCIPAL_ID = "55555555-5555-5555-5555-555555555555"


def _revision() -> dict[str, Any]:
    return {
        "id": REVISION_ID,
        "version": 1,
        "include_private": True,
        "include_tenant": False,
        "include_public": False,
        "allow_tenant_write": False,
        "allow_public_write": False,
        "default_write_visibility": "workspace",
        "default_write_workspace_id": WORKSPACE_ID,
        "created_by_principal_id": PRINCIPAL_ID,
        "reason": "initial policy",
        "created_at": "2026-07-17T00:00:00Z",
        "workspace_grants": [
            {
                "workspace_id": WORKSPACE_ID,
                "workspace_slug": "engineering",
                "can_read": True,
                "can_write": True,
            }
        ],
    }


def _profile(*, enabled: bool = True) -> dict[str, Any]:
    return {
        "id": PROFILE_ID,
        "name": "Coding",
        "slug": "coding",
        "description": "coding profile",
        "enabled": enabled,
        "active_revision_id": REVISION_ID,
        "active_revision": _revision(),
        "created_at": "2026-07-17T00:00:00Z",
        "updated_at": "2026-07-17T00:00:00Z",
    }


class Recorder:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.payload)


def _client(recorder: Recorder) -> EngramClient:
    return EngramClient(
        "http://engram.test", api_key="test", transport=httpx.MockTransport(recorder)
    )


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


async def test_profile_create_serializes_input_grants_and_parses_output_slug() -> None:
    recorder = Recorder(_profile(), status_code=201)
    request = MemoryProfileCreate(
        name="Coding",
        slug="coding",
        reason="initial policy",
        policy=MemoryProfilePolicy(
            default_write_visibility="workspace",
            default_write_workspace_id=UUID(WORKSPACE_ID),
            workspace_grants=[
                WorkspaceGrantInput(workspace_id=UUID(WORKSPACE_ID), can_read=True, can_write=True)
            ],
        ),
    )
    async with _client(recorder) as client:
        result = await client.create_memory_profile(request)

    body = _body(recorder.requests[0])
    assert body["policy"]["workspace_grants"] == [
        {"workspace_id": WORKSPACE_ID, "can_read": True, "can_write": True}
    ]
    assert "workspace_slug" not in body["policy"]["workspace_grants"][0]
    assert result.active_revision.workspace_grants[0].workspace_slug == "engineering"


async def test_list_detail_and_revision_history_preserve_server_contract() -> None:
    summary = {
        key: value for key, value in _profile(enabled=False).items() if key != "active_revision"
    }
    summary["active_revision_version"] = 1
    recorder = Recorder([summary])
    async with _client(recorder) as client:
        listed = await client.list_memory_profiles(include_disabled=True)
    assert listed[0].enabled is False
    assert recorder.requests[0].url.params["include_disabled"] == "true"

    recorder = Recorder(_profile())
    async with _client(recorder) as client:
        detail = await client.get_memory_profile(UUID(PROFILE_ID))
    assert detail.active_revision.workspace_grants[0].workspace_slug == "engineering"

    recorder = Recorder([_revision()])
    async with _client(recorder) as client:
        revisions = await client.list_memory_profile_revisions(UUID(PROFILE_ID))
    assert revisions[0].workspace_grants[0].workspace_slug == "engineering"


async def test_revision_and_lifecycle_methods_send_expected_shapes() -> None:
    recorder = Recorder(_profile())
    request = MemoryProfileRevisionCreate(
        expected_active_revision_id=UUID(REVISION_ID),
        reason="tighten",
        policy=MemoryProfilePolicy(),
    )
    async with _client(recorder) as client:
        await client.create_memory_profile_revision(UUID(PROFILE_ID), request)
    assert _body(recorder.requests[0])["expected_active_revision_id"] == REVISION_ID
    assert _body(recorder.requests[0])["policy"]["workspace_grants"] == []

    recorder = Recorder(_profile(enabled=False))
    async with _client(recorder) as client:
        await client.disable_memory_profile(UUID(PROFILE_ID), "pause")
    assert _body(recorder.requests[0]) == {"reason": "pause"}

    recorder = Recorder(_profile())
    async with _client(recorder) as client:
        await client.enable_memory_profile(UUID(PROFILE_ID), "resume")
    assert _body(recorder.requests[0]) == {"reason": "resume"}


async def test_profile_binding_forwarding_and_whoami_variants() -> None:
    key_payload = {
        "id": REVISION_ID,
        "tenant_id": TENANT_ID,
        "principal_id": PRINCIPAL_ID,
        "scopes": ["read"],
        "label": "profiled",
        "key": "eng_one-time",
        "memory_profile_id": PROFILE_ID,
        "memory_profile_revision_id": REVISION_ID,
        "memory_profile_slug": "coding",
        "memory_profile_version": 1,
    }
    recorder = Recorder(key_payload, status_code=201)
    async with _client(recorder) as client:
        await client.create_api_key(
            ApiKeyCreateRequest(tenant_id=UUID(TENANT_ID), memory_profile_id=UUID(PROFILE_ID))
        )
    assert _body(recorder.requests[0])["memory_profile_id"] == PROFILE_ID

    agent_payload = {
        "id": PRINCIPAL_ID,
        "name": "worker",
        "type": "agent",
        "created_at": "2026-07-17T00:00:00Z",
        "key": "eng_one-time",
        "key_id": REVISION_ID,
        "scopes": ["read", "write"],
        "label": "agent:worker",
        "memory_profile_id": PROFILE_ID,
        "memory_profile_revision_id": REVISION_ID,
        "memory_profile_slug": "coding",
        "memory_profile_version": 1,
    }
    recorder = Recorder(agent_payload, status_code=201)
    async with _client(recorder) as client:
        await client.create_agent(
            AgentCreateRequest(name="worker", memory_profile_id=UUID(PROFILE_ID))
        )
    assert _body(recorder.requests[0])["memory_profile_id"] == PROFILE_ID

    for memory_profile in (
        None,
        {
            "id": PROFILE_ID,
            "slug": "coding",
            "active_revision_id": REVISION_ID,
            "version": 1,
        },
    ):
        recorder = Recorder(
            {
                "principal_id": PRINCIPAL_ID,
                "principal_type": "agent",
                "tenant_id": TENANT_ID,
                "scopes": ["read"],
                "api_key_id": REVISION_ID,
                "memory_profile": memory_profile,
            }
        )
        async with _client(recorder) as client:
            whoami = await client.whoami()
        assert (whoami.memory_profile is None) is (memory_profile is None)


async def test_profile_http_failures_keep_typed_exception_behavior() -> None:
    recorder = Recorder({"detail": "memory profile not found"}, status_code=404)
    async with _client(recorder) as client:
        with pytest.raises(EngramNotFoundError):
            await client.get_memory_profile(UUID(PROFILE_ID))

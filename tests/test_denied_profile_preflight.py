"""Unit tests for the denied-profile preflight (Correction E) in stage_0.

These tests exercise the diagnostic preflight logic WITHOUT a live service or
database. They use httpx.MockTransport for the EngramAPI /whoami calls and
monkeypatch the _owner_profile_diagnostic helper to avoid needing PostgreSQL.

The preflight is a diagnostic, not a stage gate: it records checks under
``evidence.checks.denied_profile`` but never changes stage_0's pass status.
Stage 7 remains the authoritative behavioral proof.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from engram.memory_audit import RunState

# Load the CLI module (scripts/ is not a package).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "run_memory_e2e_audit.py"
_spec = importlib.util.spec_from_file_location("run_memory_e2e_audit", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
sys.modules["run_memory_e2e_audit"] = cli
_spec.loader.exec_module(cli)


# ── helpers ──────────────────────────────────────────────────────────────────


def _mkstate() -> RunState:
    return RunState(
        run_id=str(uuid.uuid4()),
        started_at=datetime.now(UTC),
        target_host="h",
    )


def _base_cfg(
    *,
    denied_key: str = "denied",
    owner_db_url: str = "postgresql+asyncpg://owner:pw@host/db",
) -> cli.AuditConfig:
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "agent"
    cfg.reviewer_key = "reviewer"
    cfg.denied_key = denied_key
    cfg.tenant_visibility_allowed = True
    cfg.owner_db_url = owner_db_url
    return cfg


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Patch cli.EngramAPI so every instance uses the given MockTransport."""
    orig_init = cli.EngramAPI.__init__

    def patched_init(self: cli.EngramAPI, base_url: str, api_key: str, **kw: Any) -> None:
        kw.pop("transport", None)
        orig_init(self, base_url, api_key, transport=transport, **kw)

    monkeypatch.setattr(cli.EngramAPI, "__init__", patched_init)


def _make_handler(
    *,
    tenant_id: str,
    agent_pid: str,
    reviewer_pid: str,
    denied_response: dict[str, Any] | None = None,
    denied_status: int = 200,
    denied_profile_fields: dict[str, Any] | None = None,
) -> Any:
    """Build a MockTransport handler for the three keys.

    ``denied_profile_fields`` is merged into the denied whoami payload to set
    memory_profile_id / memory_profile_revision_id / memory_profile_version.
    """
    agent_payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "principal_id": agent_pid,
        "principal_type": "agent",
        "api_key_id": "key-agent-001",
        "scopes": ["read", "write"],
    }
    reviewer_payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "principal_id": reviewer_pid,
        "principal_type": "user",
        "api_key_id": "key-reviewer-001",
        "scopes": ["read", "write", "review"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth == "Bearer agent":
            return httpx.Response(200, json=agent_payload)
        if auth == "Bearer reviewer":
            return httpx.Response(200, json=reviewer_payload)
        if auth == "Bearer denied":
            if denied_status >= 400:
                return httpx.Response(denied_status, json={"detail": "unauthorized"})
            payload = denied_response or {
                "tenant_id": tenant_id,
                "principal_id": str(uuid.uuid4()),
                "principal_type": "agent",
                "api_key_id": "key-denied-001",
                "scopes": ["read"],
            }
            if denied_profile_fields:
                payload = {**payload, **denied_profile_fields}
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"detail": "no mock"})

    return handler


def _denied_checks(state: RunState) -> dict[str, Any]:
    """Extract the denied_profile checks from stage_0 evidence."""
    evidence = state.stage("stage_0_identity_preflight").evidence
    checks = evidence.get("checks", {})
    return checks.get("denied_profile", {})


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_ordinary_same_tenant_key_diagnosed_as_not_restrictive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied profile with include_tenant=true is not restrictive."""
    s = _mkstate()
    tid = str(uuid.uuid4())
    profile_rev = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile_fields={
            "memory_profile_id": str(uuid.uuid4()),
            "memory_profile_revision_id": profile_rev,
            "memory_profile_version": 3,
        },
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def fake_diag(owner_url: str, profile_revision_id: str) -> dict[str, Any]:
        return {
            "available": True,
            "read_only": True,
            "include_tenant": True,
            "include_private": False,
            "include_public": True,
        }

    monkeypatch.setattr(cli, "_owner_profile_diagnostic", fake_diag)
    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    # Stage 0 still passes — preflight is diagnostic only.
    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is True
    assert dc["restrictive"] is False
    assert dc["error"] == "NEGATIVE_PROFILE_NOT_RESTRICTIVE"
    assert dc["profile_diagnostic"]["include_tenant"] is True


async def test_bound_include_tenant_false_passes_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied profile with include_tenant=false is restrictive."""
    s = _mkstate()
    tid = str(uuid.uuid4())
    profile_rev = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile_fields={
            "memory_profile_id": str(uuid.uuid4()),
            "memory_profile_revision_id": profile_rev,
            "memory_profile_version": 1,
        },
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def fake_diag(owner_url: str, profile_revision_id: str) -> dict[str, Any]:
        return {
            "available": True,
            "read_only": True,
            "include_tenant": False,
            "include_private": True,
            "include_public": True,
        }

    monkeypatch.setattr(cli, "_owner_profile_diagnostic", fake_diag)
    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is True
    assert dc["restrictive"] is True
    assert dc["has_profile"] is True
    assert dc["profile_id"] is not None
    assert dc["profile_diagnostic"]["include_tenant"] is False


async def test_different_profile_id_with_include_tenant_true_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied key with a different profile ID but include_tenant=true is rejected."""
    s = _mkstate()
    tid = str(uuid.uuid4())
    profile_rev = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile_fields={
            "memory_profile_id": str(uuid.uuid4()),  # different profile
            "memory_profile_revision_id": profile_rev,
            "memory_profile_version": 5,
        },
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def fake_diag(owner_url: str, profile_revision_id: str) -> dict[str, Any]:
        return {
            "available": True,
            "read_only": True,
            "include_tenant": True,
            "include_private": False,
            "include_public": False,
        }

    monkeypatch.setattr(cli, "_owner_profile_diagnostic", fake_diag)
    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["restrictive"] is False
    assert dc["error"] == "NEGATIVE_PROFILE_NOT_RESTRICTIVE"


async def test_invalid_denied_credential_distinct_from_restrictive_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 auth failure on the denied key records a credential error, not a profile error."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_status=401,
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    # Stage 0 still passes — the agent/reviewer identity is valid.
    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is False
    assert dc["error"] == "NEGATIVE_CONTROL_CREDENTIAL_INVALID"
    # Must NOT be the profile-restrictive error.
    assert dc.get("restrictive") is None or "restrictive" not in dc


async def test_no_owner_diagnostics_records_unproven(monkeypatch: pytest.MonkeyPatch) -> None:
    """When owner_db_url is unset, the restrictive policy is recorded as unproven."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile_fields={
            "memory_profile_id": str(uuid.uuid4()),
            "memory_profile_revision_id": str(uuid.uuid4()),
            "memory_profile_version": 2,
        },
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    cfg = _base_cfg(owner_db_url="")  # no owner diagnostics

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is True
    assert dc["restrictive"] is None
    assert dc["error"] == "NEGATIVE_PROFILE_POLICY_UNPROVEN"
    assert "profile_diagnostic" not in dc

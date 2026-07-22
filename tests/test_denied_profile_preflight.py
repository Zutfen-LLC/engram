"""Unit tests for the denied-profile preflight (Blocker A) in stage_0.

These tests exercise the diagnostic preflight logic WITHOUT a live service or
database. They use httpx.MockTransport for the EngramAPI /whoami calls and
monkeypatch the _owner_profile_diagnostic helper to avoid needing PostgreSQL.

The tests use the REAL nested ``/whoami.memory_profile`` contract:

    {
        "principal_id": "...",
        "principal_type": "agent",
        "tenant_id": "...",
        "scopes": ["read"],
        "api_key_id": "...",
        "memory_profile": {
            "id": "...",
            "slug": "restricted-audit",
            "active_revision_id": "...",
            "version": 1
        }
    }

NOT the legacy flattened fields (memory_profile_id, etc.) that the FIX3 code
incorrectly relied on.

The preflight is a diagnostic, not a stage gate: it records checks under
``evidence.checks.denied_profile`` but never changes stage_0's pass status.
Stage 7 remains the authoritative behavioral proof, and its enforcement of
the preflight is tested separately in test_memory_e2e_audit.py.
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


def _nested_profile(
    *,
    profile_id: str | None = None,
    slug: str = "audit-denied",
    revision_id: str | None = None,
    version: int = 1,
) -> dict[str, Any] | None:
    """Build the real nested ``memory_profile`` object from /whoami."""
    if profile_id is None or revision_id is None:
        return {
            "id": str(uuid.uuid4()),
            "slug": slug,
            "active_revision_id": str(uuid.uuid4()),
            "version": version,
        }
    return {
        "id": profile_id,
        "slug": slug,
        "active_revision_id": revision_id,
        "version": version,
    }


def _make_handler(
    *,
    tenant_id: str,
    agent_pid: str,
    reviewer_pid: str,
    denied_tenant_id: str | None = None,
    denied_api_key_id: str = "key-denied-001",
    denied_profile: dict[str, Any] | None = None,
    denied_status: int = 200,
) -> Any:
    """Build a MockTransport handler using the REAL nested /whoami shape.

    ``denied_profile`` is the nested ``memory_profile`` dict (or None).
    ``denied_tenant_id`` defaults to ``tenant_id`` (same tenant).
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
    actual_denied_tenant = denied_tenant_id or tenant_id

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth == "Bearer agent":
            return httpx.Response(200, json=agent_payload)
        if auth == "Bearer reviewer":
            return httpx.Response(200, json=reviewer_payload)
        if auth == "Bearer denied":
            if denied_status >= 400:
                return httpx.Response(denied_status, json={"detail": "unauthorized"})
            payload: dict[str, Any] = {
                "tenant_id": actual_denied_tenant,
                "principal_id": str(uuid.uuid4()),
                "principal_type": "agent",
                "api_key_id": denied_api_key_id,
                "scopes": ["read"],
            }
            if denied_profile is not None:
                payload["memory_profile"] = denied_profile
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"detail": "no mock"})

    return handler


def _denied_checks(state: RunState) -> dict[str, Any]:
    """Extract the denied_profile checks from stage_0 evidence."""
    evidence = state.stage("stage_0_identity_preflight").evidence
    checks = evidence.get("checks", {})
    return checks.get("denied_profile", {})


# ── 1. Nested profile parser tests ───────────────────────────────────────────


def test_nested_profile_parses_correctly() -> None:
    """Nested ``/whoami.memory_profile`` parses to a WhoAmIProfile."""
    profile_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    identity = {
        "tenant_id": "t1",
        "principal_id": "p1",
        "principal_type": "agent",
        "api_key_id": "k1",
        "scopes": ["read"],
        "memory_profile": {
            "id": profile_id,
            "slug": "restricted-audit",
            "active_revision_id": rev_id,
            "version": 2,
        },
    }
    result = cli._whoami_profile(identity)
    assert result is not None
    assert result.profile_id == profile_id
    assert result.slug == "restricted-audit"
    assert result.active_revision_id == rev_id
    assert result.version == 2


def test_missing_profile_returns_none() -> None:
    """When ``memory_profile`` is absent, parser returns None."""
    identity = {
        "tenant_id": "t1",
        "principal_id": "p1",
        "api_key_id": "k1",
        "scopes": ["read"],
    }
    assert cli._whoami_profile(identity) is None


def test_partial_nested_profile_rejected() -> None:
    """A partial nested profile object raises ValueError."""
    identity = {
        "tenant_id": "t1",
        "memory_profile": {
            "id": str(uuid.uuid4()),
            "slug": "partial",
            # active_revision_id missing
            "version": 1,
        },
    }
    with pytest.raises(ValueError, match="missing required fields"):
        cli._whoami_profile(identity)


def test_nested_profile_not_dict_rejected() -> None:
    """When ``memory_profile`` is not a dict, raises ValueError."""
    identity = {
        "tenant_id": "t1",
        "memory_profile": "not-a-dict",
    }
    with pytest.raises(ValueError, match="not an object"):
        cli._whoami_profile(identity)


def test_parser_does_not_read_flattened_fields() -> None:
    """Parser must NOT read legacy flattened fields even if present."""
    identity = {
        "tenant_id": "t1",
        "memory_profile_id": str(uuid.uuid4()),
        "memory_profile_revision_id": str(uuid.uuid4()),
        "memory_profile_version": 3,
    }
    # Must return None — the flattened fields are not the real contract.
    assert cli._whoami_profile(identity) is None


# ── 2-7. Denied-profile preflight behavioral tests ──────────────────────────


async def test_include_tenant_true_rejected_as_not_restrictive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied profile with include_tenant=true is not restrictive."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile=_nested_profile(),
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

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is True
    assert dc["restrictive"] is False
    assert dc["error"] == "NEGATIVE_PROFILE_NOT_RESTRICTIVE"
    assert dc["profile_diagnostic"]["include_tenant"] is True


async def test_include_tenant_false_ready_for_stage_7(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied profile with include_tenant=false is restrictive and ready."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile=_nested_profile(),
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
    assert dc["same_tenant"] is True
    assert dc["distinct_key_id"] is True
    assert dc["has_profile"] is True
    assert dc["restrictive"] is True
    assert dc["ready_for_stage_7"] is True
    assert dc["policy_proven"] is True
    assert dc["include_tenant"] is False
    assert dc["profile"]["id"] is not None
    assert dc["profile"]["slug"] == "audit-denied"
    assert dc["profile"]["active_revision_id"] is not None
    assert dc["profile"]["version"] == 1


async def test_cross_tenant_key_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-tenant key is rejected with TENANT_MISMATCH."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_tenant_id=str(uuid.uuid4()),  # different tenant
        denied_profile=_nested_profile(),
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["same_tenant"] is False
    assert dc["restrictive"] is False
    assert dc["error"] == "NEGATIVE_CONTROL_TENANT_MISMATCH"


async def test_same_key_id_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied key with the same api_key_id as the agent is rejected."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_api_key_id="key-agent-001",  # same as agent
        denied_profile=_nested_profile(),
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["distinct_key_id"] is False
    assert dc["restrictive"] is False
    assert dc["error"] == "NEGATIVE_CONTROL_KEY_COLLISION"


async def test_unprofiled_key_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key with no memory_profile bound is rejected as NOT_BOUND."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile=None,  # no nested memory_profile
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = _base_cfg()

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["has_profile"] is False
    assert dc["restrictive"] is False
    assert dc["error"] == "NEGATIVE_PROFILE_NOT_BOUND"


async def test_invalid_credential_diagnosed_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 auth failure on the denied key records a credential error."""
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

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is False
    assert dc["error"] == "NEGATIVE_CONTROL_CREDENTIAL_INVALID"


async def test_owner_diagnostics_absent_records_unproven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When owner_db_url is unset, restrictive policy is unproven."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
        denied_profile=_nested_profile(),
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    cfg = _base_cfg(owner_db_url="")

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    dc = _denied_checks(s)
    assert dc["authenticated"] is True
    assert dc["has_profile"] is True
    assert dc["restrictive"] is None
    assert dc["error"] == "NEGATIVE_PROFILE_POLICY_UNPROVEN"
    assert "profile_diagnostic" not in dc
    assert "ready_for_stage_7" not in dc


async def test_stage_0_passes_without_denied_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 0 passes when denied key is absent (optional lane)."""
    s = _mkstate()
    tid = str(uuid.uuid4())

    handler = _make_handler(
        tenant_id=tid,
        agent_pid=str(uuid.uuid4()),
        reviewer_pid=str(uuid.uuid4()),
    )
    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = _base_cfg()
    cfg.denied_key = ""

    await cli.stage_0_identity_preflight(s, cfg)

    assert s.stage("stage_0_identity_preflight").status == "pass"
    # denied_profile checks should not be present at all.
    dc = _denied_checks(s)
    assert dc == {}


# ── 8. Safe identity uses nested profile ─────────────────────────────────────


def test_safe_identity_retains_nested_profile() -> None:
    """_safe_identity preserves the nested profile identity."""
    profile_id = str(uuid.uuid4())
    rev_id = str(uuid.uuid4())
    who = {
        "tenant_id": "t1",
        "principal_id": "p1",
        "principal_type": "agent",
        "api_key_id": "k1",
        "scopes": ["read"],
        "memory_profile": {
            "id": profile_id,
            "slug": "test-profile",
            "active_revision_id": rev_id,
            "version": 1,
        },
    }
    safe = cli._safe_identity(who)
    assert "memory_profile" in safe
    assert safe["memory_profile"]["id"] == profile_id
    assert safe["memory_profile"]["slug"] == "test-profile"
    assert safe["memory_profile"]["active_revision_id"] == rev_id
    assert safe["memory_profile"]["version"] == 1
    # No credentials retained.
    assert "api_key" not in safe
    assert "authorization" not in safe


def test_safe_identity_no_profile_when_absent() -> None:
    """_safe_identity omits profile fields when absent."""
    who = {
        "tenant_id": "t1",
        "principal_id": "p1",
        "api_key_id": "k1",
        "scopes": ["read"],
    }
    safe = cli._safe_identity(who)
    assert "memory_profile" not in safe
    assert "memory_profile_error" not in safe

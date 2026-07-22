"""Identity-drift tests for ENG-AUDIT-001-FIX5 Part B.

Tests that Stage 7 verifies the denied-profile credential is the exact same
identity and profile revision validated during Stage 0. Uses the real nested
``/whoami.memory_profile`` shape via httpx.MockTransport.
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


TENANT = str(uuid.uuid4())
AGENT_PRINCIPAL = str(uuid.uuid4())
REVIEWER_PRINCIPAL = str(uuid.uuid4())
DENIED_PRINCIPAL = str(uuid.uuid4())
DENIED_API_KEY_ID = str(uuid.uuid4())
DENIED_PROFILE_ID = str(uuid.uuid4())
DENIED_PROFILE_SLUG = "audit-denied"
DENIED_REVISION_ID = str(uuid.uuid4())
DENIED_PROFILE_VERSION = 1

# Sentinel: memory_profile key absent from /whoami response
_MISSING = object()


def _mkstate() -> RunState:
    s = RunState(
        run_id=str(uuid.uuid4()),
        started_at=datetime.now(UTC),
        target_host="h",
    )
    # Set stage 0 as passed
    s.stage("stage_0_identity_preflight").status = "pass"
    s.stage("stage_0_identity_preflight").completed_at = datetime.now(UTC)
    return s


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
    orig_init = cli.EngramAPI.__init__

    def patched_init(self: cli.EngramAPI, base_url: str, api_key: str, **kw: Any) -> None:
        kw.pop("transport", None)
        orig_init(self, base_url, api_key, transport=transport, **kw)

    monkeypatch.setattr(cli.EngramAPI, "__init__", patched_init)


def _nested_profile(
    *,
    profile_id: str | None = DENIED_PROFILE_ID,
    slug: str = DENIED_PROFILE_SLUG,
    revision_id: str | None = DENIED_REVISION_ID,
    version: int = DENIED_PROFILE_VERSION,
) -> dict[str, Any] | None:
    if profile_id is None or revision_id is None:
        return None
    return {
        "id": profile_id,
        "slug": slug,
        "active_revision_id": revision_id,
        "version": version,
    }


def _denied_whoami(
    *,
    tenant_id: str = TENANT,
    principal_id: str = DENIED_PRINCIPAL,
    api_key_id: str = DENIED_API_KEY_ID,
    memory_profile: dict[str, Any] | None | object = _MISSING,
    principal_type: str = "agent",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "api_key_id": api_key_id,
        "scopes": scopes or ["read", "write"],
    }
    if memory_profile != "MISSING":
        result["memory_profile"] = memory_profile
    return result


def _agent_whoami() -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "principal_id": AGENT_PRINCIPAL,
        "principal_type": "agent",
        "api_key_id": str(uuid.uuid4()),
        "scopes": ["read", "write"],
    }


def _reviewer_whoami() -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "principal_id": REVIEWER_PRINCIPAL,
        "principal_type": "user",
        "api_key_id": str(uuid.uuid4()),
        "scopes": ["read", "write", "review"],
    }


def _setup_stage_0_passed(state: RunState, denied_whoami_response: dict[str, Any]) -> None:
    """Set up Stage 0 state as if it passed with the given denied identity."""
    denied_checks = {
        "authenticated": True,
        "same_tenant": denied_whoami_response.get("tenant_id") == TENANT,
        "distinct_key_id": (
            denied_whoami_response.get("api_key_id")
            != _agent_whoami().get("api_key_id")
        ),
        "has_profile": denied_whoami_response.get("memory_profile") is not None,
        "restrictive": True,
        "ready_for_stage_7": True,
        "policy_proven": True,
        "include_tenant": False,
        "proven_identity": cli._denied_identity_record(denied_whoami_response),
    }
    state.stage("stage_0_identity_preflight").evidence["checks"] = {
        "denied_profile": denied_checks,
    }


def _make_transport(
    *,
    denied_whoami: dict[str, Any] | None = None,
    agent_whoami: dict[str, Any] | None = None,
    reviewer_whoami: dict[str, Any] | None = None,
    denied_item_response: tuple[int, dict[str, Any]] = (403, {"detail": "forbidden"}),
    denied_recall_response: tuple[int, dict[str, Any]] = (
        200,
        {"items": [], "recall_log_id": str(uuid.uuid4())},
    ),
) -> httpx.MockTransport:
    aw = agent_whoami or _agent_whoami()
    rw = reviewer_whoami or _reviewer_whoami()
    dw = denied_whoami

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/whoami":
            # Determine which key this is — use a header or just respond
            # based on the Bearer token prefix
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer denied"):
                if dw is None:
                    return httpx.Response(401, json={"detail": "invalid"})
                return httpx.Response(200, json=dw)
            elif auth.startswith("Bearer agent"):
                return httpx.Response(200, json=aw)
            elif auth.startswith("Bearer reviewer"):
                return httpx.Response(200, json=rw)
            return httpx.Response(401, json={"detail": "unknown key"})
        # For item GET requests from denied key
        if (
            request.method == "GET"
            and path.startswith("/v1/items/")
            and "Bearer denied" in request.headers.get("Authorization", "")
        ):
            status, payload = denied_item_response
            return httpx.Response(status, json=payload)
        # For recall from denied key
        if (
            path == "/v1/recall"
            and "Bearer denied" in request.headers.get("Authorization", "")
        ):
            status, payload = denied_recall_response
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"detail": "no mock"})

    return httpx.MockTransport(handler)


# ── Test 1: Same denied key and profile passes continuity ───────────────────


async def test_same_denied_key_passes_continuity(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    transport = _make_transport(denied_whoami=dw)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "pass_expected_denial"


# ── Test 2: Different API key ID blocks ──────────────────────────────────────


async def test_different_api_key_id_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    # Stage 7 sees a different api_key_id
    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(),
        api_key_id=str(uuid.uuid4()),  # different key
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 3: Different tenant blocks ──────────────────────────────────────────


async def test_different_tenant_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(),
        tenant_id=str(uuid.uuid4()),  # different tenant
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 4: Different principal blocks ───────────────────────────────────────


async def test_different_principal_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(),
        principal_id=str(uuid.uuid4()),  # different principal
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 5: Missing profile at Stage 7 blocks ────────────────────────────────


async def test_missing_profile_at_stage_7_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(memory_profile=None)  # no profile
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 6: Different profile ID blocks ──────────────────────────────────────


async def test_different_profile_id_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(profile_id=str(uuid.uuid4())),
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 7: Different profile slug blocks ────────────────────────────────────


async def test_different_profile_slug_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(slug="different-slug"),
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 8: Different active revision ID blocks ─────────────────────────────


async def test_different_revision_id_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(revision_id=str(uuid.uuid4())),
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 9: Different profile version blocks ────────────────────────────────


async def test_different_profile_version_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(version=2),  # changed version
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 10: Malformed nested profile at Stage 7 blocks ─────────────────────


async def test_malformed_nested_profile_at_stage_7_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    dw_stage7 = _denied_whoami(
        memory_profile={"id": DENIED_PROFILE_ID},  # missing required fields
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 11: Stage 7 does not call get_item after drift ─────────────────────


async def test_no_get_item_after_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    item_id = str(uuid.uuid4())
    s.fixture("recall").item_id = item_id
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    item_calls: list[str] = []

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(),
        api_key_id=str(uuid.uuid4()),  # drift
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        auth = request.headers.get("Authorization", "")
        if path == "/whoami":
            if "Bearer denied" in auth:
                return httpx.Response(200, json=dw_stage7)
            elif "Bearer agent" in auth:
                return httpx.Response(200, json=_agent_whoami())
            elif "Bearer reviewer" in auth:
                return httpx.Response(200, json=_reviewer_whoami())
            return httpx.Response(401, json={"detail": "unknown"})
        if request.method == "GET" and path.startswith("/v1/items/"):
            if "Bearer denied" in auth:
                item_calls.append(path)
            return httpx.Response(403, json={"detail": "forbidden"})
        if path == "/v1/recall":
            return httpx.Response(200, json={"items": [], "recall_log_id": str(uuid.uuid4())})
        return httpx.Response(404, json={"detail": "no mock"})

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    # Verify no get_item was called for the denied key
    assert len(item_calls) == 0, f"get_item was called after drift: {item_calls}"


# ── Test 12: Stage 7 does not call recall after drift ───────────────────────


async def test_no_recall_after_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    item_id = str(uuid.uuid4())
    s.fixture("recall").item_id = item_id
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    recall_calls: list[str] = []

    dw_stage7 = _denied_whoami(
        memory_profile=_nested_profile(),
        api_key_id=str(uuid.uuid4()),  # drift
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        auth = request.headers.get("Authorization", "")
        if path == "/whoami":
            if "Bearer denied" in auth:
                return httpx.Response(200, json=dw_stage7)
            elif "Bearer agent" in auth:
                return httpx.Response(200, json=_agent_whoami())
            elif "Bearer reviewer" in auth:
                return httpx.Response(200, json=_reviewer_whoami())
            return httpx.Response(401, json={"detail": "unknown"})
        if request.method == "GET" and path.startswith("/v1/items/"):
            return httpx.Response(403, json={"detail": "forbidden"})
        if path == "/v1/recall":
            if "Bearer denied" in auth:
                recall_calls.append(path)
            return httpx.Response(200, json={"items": [], "recall_log_id": str(uuid.uuid4())})
        return httpx.Response(404, json={"detail": "no mock"})

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    assert len(recall_calls) == 0, f"recall was called after drift: {recall_calls}"


# ── Test 13: Cross-tenant replacement key cannot pass through 404 ───────────


async def test_cross_tenant_replacement_key_cannot_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    # A completely different cross-tenant key that returns 404 for items
    dw_stage7 = _denied_whoami(
        tenant_id=str(uuid.uuid4()),
        api_key_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        memory_profile=_nested_profile(),
    )
    transport = _make_transport(
        denied_whoami=dw_stage7,
        denied_item_response=(404, {"detail": "not found"}),
    )
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    # Must be blocked by identity drift, not pass as "expected denial"
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 14: Ordinary unprofiled replacement key cannot pass ────────────────


async def test_unprofiled_replacement_key_cannot_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw_stage0 = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw_stage0)
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    # Replacement key with no profile
    dw_stage7 = _denied_whoami(
        memory_profile=None,
        api_key_id=str(uuid.uuid4()),
    )
    transport = _make_transport(denied_whoami=dw_stage7)
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "blocked"
    assert neg.reason_code == "NEGATIVE_CONTROL_IDENTITY_DRIFT"


# ── Test 15: Valid unchanged restrictive key still passes full behavioral ───


async def test_unchanged_restrictive_key_passes_behavioral(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    dw = _denied_whoami(memory_profile=_nested_profile())
    _setup_stage_0_passed(s, dw)
    item_id = str(uuid.uuid4())
    s.fixture("recall").item_id = item_id
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"

    # Same key, same profile — should pass direct denial + recall omission
    transport = _make_transport(
        denied_whoami=dw,
        denied_item_response=(403, {"detail": "forbidden"}),
        denied_recall_response=(200, {"items": [], "recall_log_id": str(uuid.uuid4())}),
    )
    _install_mock_transport(monkeypatch, transport)
    cfg = _base_cfg()
    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_r_denied_profile")
    assert neg.status == "pass_expected_denial"
    assert neg.reason_code == "PASS_EXPECTED_DENIAL"

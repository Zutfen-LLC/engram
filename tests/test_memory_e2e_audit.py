"""Unit + mock-transport tests for the deterministic memory E2E audit harness.

These tests exercise the framework-independent core (``engram.memory_audit``)
and the CLI/httpx client (``scripts.run_memory_e2e_audit``) WITHOUT a live
service, using :class:`httpx.MockTransport`. They cover:

* run-state serialization + resumability;
* schema validation;
* secret redaction + the on-disk secret assertion;
* reason-code mapping + pass-vs-expected-denial semantics;
* duplicate marker handling;
* report aggregation;
* the mock-transport stage paths (identity, item not found, duplicate, fixture
  creation + governed activation, recall success/missing, negative controls);
* cleanup item-id safety + no API keys persisted.

The real-PostgreSQL promotion/RLS proofs live in
``tests/test_memory_e2e_audit_postgres.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from engram.memory_audit import (
    RunState,
    assert_no_secrets,
    finalize_report,
    load_schema,
    load_state,
    redact_secrets,
    sanitize_host,
    save_state,
    validate_report,
)

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


def _mkstate(marker_write: str = "AUDIT-WRITE-x") -> RunState:
    s = RunState(run_id=str(uuid.uuid4()), started_at=datetime.now(UTC), target_host="h")
    s.fixture("write").marker = marker_write
    # Individual command tests exercise the boundary after its required Stage
    # 0 gate; dedicated identity tests overwrite this state themselves.
    s.stage("stage_0_identity_preflight").status = "pass"
    return s


def _dict_handler(routes: dict[str, Any]) -> Any:
    """Build a MockTransport handler from {method path: (status, payload)}."""

    def handler(request: httpx.Request) -> httpx.Response:
        # normalize: strip query, match on method+path
        path = request.url.path
        key = f"{request.method} {path}"
        # also try path-prefix matches for templated item ids
        for k, v in routes.items():
            m, p = k.split(" ", 1)
            if m != request.method:
                continue
            if p == path or p.rstrip("/") == path.rstrip("/"):
                status, payload = v
                return httpx.Response(status, json=payload)
        # fallback: scan for prefix templates
        for k, v in routes.items():
            m, p = k.split(" ", 1)
            if m != request.method:
                continue
            if "{item_id}" in p:
                prefix = p.split("{item_id}")[0]
                if path.startswith(prefix):
                    status, payload = v
                    return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"detail": f"no mock for {key}"})

    return handler


# ── run-state serialization ──────────────────────────────────────────────────


def test_run_state_roundtrip_preserves_stages_and_fixtures(tmp_path: Path) -> None:
    s = _mkstate()
    s.stage("stage_0_identity_preflight").status = "pass"
    s.fixture("recall").item_id = str(uuid.uuid4())
    s.fixture("recall").marker = "AUDIT-RECALL-x"

    save_state(s, tmp_path)
    loaded = load_state(tmp_path / s.run_id)
    assert loaded.run_id == s.run_id
    assert loaded.fixture("recall").item_id == s.fixture("recall").item_id
    assert loaded.stage("stage_0_identity_preflight").status == "pass"
    assert loaded.fixture("write").marker == "AUDIT-WRITE-x"


def test_run_state_is_secret_free_on_disk(tmp_path: Path) -> None:
    s = _mkstate()
    # Simulate operator evidence that accidentally contains a secret.
    raw_bearer = "Bearer abc123def456ghi789jkl012mno"
    raw_key = "eng_AbcDef123456_LongSecretMaterialHereXYZ"
    s.operator_evidence["hermes_response"] = f"Authorization: {raw_bearer}"
    s.identity = {"agent": {"token": raw_key}}
    from engram.memory_audit import save_state

    save_state(s, tmp_path)  # redaction runs before write
    state_file = tmp_path / s.run_id / "state.json"
    on_disk = state_file.read_text(encoding="utf-8")
    # The raw secret material must NEVER appear on disk.
    assert raw_bearer not in on_disk
    assert "LongSecretMaterialHereXYZ" not in on_disk
    # Redaction replaced it.
    assert "REDACTED" in on_disk


def test_assert_no_secrets_backstop_catches_unredacted() -> None:
    # The defense-in-depth assertion must fire for a secret-shaped string
    # that somehow reached the serialization path unredacted.
    with pytest.raises(AssertionError, match="secret-shaped"):
        assert_no_secrets("postgresql+asyncpg://u:s3cretPwXYZ@host/db", context="backstop")


def test_resumability_partial_run_resumes_without_restarting(tmp_path: Path) -> None:
    s = _mkstate()
    s.stage("stage_0_identity_preflight").status = "pass"
    s.stage("stage_0_identity_preflight").completed_at = datetime.now(UTC)

    save_state(s, tmp_path)
    loaded = load_state(tmp_path / s.run_id)
    # Resuming: stage_0 is already pass, must not be re-run.
    assert loaded.stage("stage_0_identity_preflight").status == "pass"
    # A new stage can be started independently.
    loaded.stage("stage_1_hermes_write").status = "blocked"
    save_state(loaded, tmp_path)
    again = load_state(tmp_path / s.run_id)
    assert again.stage("stage_1_hermes_write").status == "blocked"
    assert again.stage("stage_0_identity_preflight").status == "pass"


# ── schema validation ─────────────────────────────────────────────────────────


def test_report_validates_against_schema() -> None:
    s = _mkstate()
    s.stage("stage_0_identity_preflight").status = "pass"
    s.stage("stage_1_hermes_write").status = "pass"
    report = finalize_report(s)
    d = report.to_dict()
    validate_report(d)  # must not raise


def test_report_rejects_wrong_schema_name() -> None:
    s = _mkstate()
    report = finalize_report(s)
    d = report.to_dict()
    d["schema"] = "wrong"
    with pytest.raises((ValueError, Exception)):  # noqa: B017, PT011
        validate_report(d)


def test_load_schema_has_required_reason_code_vocab() -> None:
    schema = load_schema()
    codes = set(schema["$defs"]["reason_code"]["enum"])
    # spot-check a representative subset from the spec
    for code in (
        "HERMES_WRITE_NOT_SUBMITTED",
        "ENGRAM_DUPLICATE_ITEMS",
        "NATIVE_HERMES_WRITE_DETECTED",
        "PASS_EXPECTED_DENIAL",
        "AGENT_ITEM_ACCESS_DENIED",
        "EXPECTED_ITEM_NOT_SELECTED",
        "RECALL_LABEL_MISMATCH",
        "MODEL_OMITTED_MARKER",
        "MODEL_TREATED_ACTIVE_AS_VERIFIED",
        "TAXONOMY_CONFIDENCE_BELOW_MINIMUM",
        "PROCESSING_FIELDS_OBSERVED",
        "EPISTEMIC_POSITIVE_EVIDENCE_MISSING",
    ):
        assert code in codes, f"missing reason code: {code}"


# ── secret redaction ──────────────────────────────────────────────────────────


def test_redact_strips_api_key_and_bearer_and_dsn() -> None:
    text = (
        "key=eng_AbCdEf123456_LongSecretMaterial tail; Bearer abc123def456; "
        "postgresql://u:s3cretPw@host/db"
    )
    out = redact_secrets(text)
    assert "LongSecretMaterial" not in out
    assert "abc123def456" not in out
    assert "s3cretPw" not in out
    assert "REDACTED" in out


def test_assert_no_secrets_passes_for_clean_audit_marker() -> None:
    # Audit markers must never trigger the secret assertion.
    assert_no_secrets(f"AUDIT-WRITE-{uuid.uuid4()}", context="marker")
    assert_no_secrets(f"engram write-audit marker is AUDIT-RECALL-{uuid.uuid4()}", context="marker")


def test_assert_no_secrets_catches_bearer() -> None:
    with pytest.raises(AssertionError, match="secret-shaped"):
        assert_no_secrets("Authorization: Bearer abc123def456ghi789", context="leak")


# ── reason-code mapping + pass/finding/failed semantics ───────────────────────


def test_pass_vs_finding_vs_failed_overall() -> None:
    # all pass -> pass
    s = _mkstate()
    for st in cli.STAGE_ORDER:
        s.stage(st).status = "pass"
    r = finalize_report(s).to_dict()
    assert r["overall"]["status"] == "pass"

    # one finding -> partial, finding recorded
    s2 = _mkstate()
    s2.stage("stage_0_identity_preflight").status = "pass"
    ev = s2.stage("stage_2_processing_promotion")
    ev.status = "finding"
    ev.reason_code = "TAXONOMY_CONFIDENCE_BELOW_MINIMUM"
    r2 = finalize_report(s2).to_dict()
    assert r2["overall"]["status"] == "partial"
    assert not r2["overall"]["failed_stages"]
    assert any("TAXONOMY_CONFIDENCE_BELOW_MINIMUM" in f for f in r2["overall"]["findings"])

    # one failed -> failed
    s3 = _mkstate()
    s3.stage("stage_0_identity_preflight").status = "failed"
    s3.stage("stage_0_identity_preflight").reason_code = "IDENTITY_AUTH_FAILED"
    r3 = finalize_report(s3).to_dict()
    assert r3["overall"]["status"] == "failed"
    assert "stage_0_identity_preflight" in r3["overall"]["failed_stages"]


def test_pass_expected_denial_is_not_a_failure() -> None:
    s = _mkstate()
    s.negative("negative_w_reviewer_private").status = "pass_expected_denial"
    s.negative("negative_w_reviewer_private").reason_code = "PASS_EXPECTED_DENIAL"
    for st in cli.STAGE_ORDER:
        s.stage(st).status = "pass"
    r = finalize_report(s).to_dict()
    assert r["overall"]["status"] == "pass"
    assert not r["overall"]["failed_stages"]


def test_empty_run_is_partial_not_pass() -> None:
    s = _mkstate()
    r = finalize_report(s).to_dict()
    assert r["overall"]["status"] == "partial"


# ── duplicate marker handling ─────────────────────────────────────────────────


async def test_verify_hermes_write_flags_duplicate_items(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate(marker_write="AUDIT-WRITE-DUP")
    s.fixture("write").marker = "AUDIT-WRITE-DUP"

    routes = {
        "GET /v1/items": (
            200,
            {
                "items": [
                    {
                        "id": str(uuid.uuid4()),
                        "content": "x AUDIT-WRITE-DUP",
                        "review_status": "proposed",
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "content": "y AUDIT-WRITE-DUP",
                        "review_status": "proposed",
                    },
                ],
                "total": 2,
            },
        ),
    }
    transport = httpx.MockTransport(_dict_handler(routes))
    _install_mock_transport(monkeypatch, transport)

    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "k"
    await cli.cmd_verify_hermes_write(s, cfg)
    assert s.stage("stage_1_hermes_write").status == "failed"
    assert s.stage("stage_1_hermes_write").reason_code == "ENGRAM_DUPLICATE_ITEMS"


# ── mock-transport stage tests ────────────────────────────────────────────────


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Patch cli.EngramAPI so every instance uses the given MockTransport."""
    orig_init = cli.EngramAPI.__init__

    def patched_init(self: cli.EngramAPI, base_url: str, api_key: str, **kw: Any) -> None:
        kw.pop("transport", None)
        orig_init(self, base_url, api_key, transport=transport, **kw)

    monkeypatch.setattr(cli.EngramAPI, "__init__", patched_init)


async def test_identity_preflight_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    tid = str(uuid.uuid4())
    agent_pid = str(uuid.uuid4())
    reviewer_pid = str(uuid.uuid4())

    # Two whoami calls (agent + reviewer) with different principal ids.
    call = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call["n"] += 1
        if call["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "tenant_id": tid,
                    "principal_id": agent_pid,
                    "principal_type": "agent",
                    "scopes": ["read", "write"],
                },
            )
        return httpx.Response(
            200,
            json={
                "tenant_id": tid,
                "principal_id": reviewer_pid,
                "principal_type": "user",
                "scopes": ["read", "write", "review"],
            },
        )

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    monkeypatch.setenv(cli.ENV_BASE_URL, "http://test")
    monkeypatch.setenv(cli.ENV_AGENT_KEY, "k")
    monkeypatch.setenv(cli.ENV_REVIEWER_KEY, "k")
    monkeypatch.setenv(cli.ENV_TENANT_ALLOWED, "true")
    cfg = cli.AuditConfig()

    await cli.stage_0_identity_preflight(s, cfg)
    assert s.stage("stage_0_identity_preflight").status == "pass"


async def test_identity_preflight_fails_on_tenant_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    call = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call["n"] += 1
        if call["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "tenant_id": str(uuid.uuid4()),
                    "principal_id": str(uuid.uuid4()),
                    "scopes": ["read", "write"],
                },
            )
        return httpx.Response(
            200,
            json={
                "tenant_id": str(uuid.uuid4()),
                "principal_id": str(uuid.uuid4()),
                "scopes": ["read", "write", "review"],
            },
        )

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    monkeypatch.setenv(cli.ENV_BASE_URL, "http://test")
    monkeypatch.setenv(cli.ENV_AGENT_KEY, "k")
    monkeypatch.setenv(cli.ENV_REVIEWER_KEY, "k")
    monkeypatch.setenv(cli.ENV_TENANT_ALLOWED, "true")

    await cli.stage_0_identity_preflight(s, cli.AuditConfig())
    assert s.stage("stage_0_identity_preflight").status == "failed"
    assert s.stage("stage_0_identity_preflight").reason_code == "IDENTITY_TENANT_MISMATCH"


async def test_identity_preflight_rejects_agent_admin_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _mkstate()
    tid = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer agent":
            return httpx.Response(
                200,
                json={
                    "tenant_id": tid,
                    "principal_id": str(uuid.uuid4()),
                    "principal_type": "agent",
                    "scopes": ["admin"],
                },
            )
        return httpx.Response(
            200,
            json={
                "tenant_id": tid,
                "principal_id": str(uuid.uuid4()),
                "principal_type": "user",
                "scopes": ["review"],
            },
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setenv(cli.ENV_BASE_URL, "http://test")
    monkeypatch.setenv(cli.ENV_AGENT_KEY, "agent")
    monkeypatch.setenv(cli.ENV_REVIEWER_KEY, "reviewer")
    monkeypatch.setenv(cli.ENV_TENANT_ALLOWED, "true")
    await cli.stage_0_identity_preflight(s, cli.AuditConfig())
    assert (
        s.stage("stage_0_identity_preflight").reason_code == "IDENTITY_AGENT_REVIEW_SCOPE_FORBIDDEN"
    )


async def test_verify_hermes_write_requires_native_and_acknowledgement_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _mkstate()
    marker = s.fixture("write").marker
    item_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": item_id,
                        "content": f"the marker is {marker}",
                        "source_type": "sync_turn",
                        "visibility": "private",
                        "review_status": "proposed",
                    }
                ],
                "next_cursor": None,
            },
        )

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    cfg = cli.AuditConfig()
    cfg.base_url, cfg.agent_key, cfg.native_paths = "http://test", "agent", []
    await cli.cmd_verify_hermes_write(s, cfg)
    assert s.stage("stage_1_hermes_write").status == "blocked"
    assert s.stage("stage_1_hermes_write").reason_code == "NATIVE_MEMORY_PROOF_UNAVAILABLE"


async def test_recall_fixture_governed_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    item_id = str(uuid.uuid4())
    cls_run = str(uuid.uuid4())
    ingest = str(uuid.uuid4())
    corr = str(uuid.uuid4())
    marker = f"AUDIT-RECALL-{s.run_id}"

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1/classify":
            return httpx.Response(
                200,
                json={
                    "classification_run_id": cls_run,
                    "ingest_id": ingest,
                    "correlation_id": corr,
                    "suggested_kind": "fact",
                    "taxonomy_confidence": 0.8,
                    "retention_confidence": 0.9,
                    "retention_disposition": "retain",
                    "reason": "ok",
                },
            )
        if p == "/v1/remember":
            return httpx.Response(
                201,
                json={
                    "id": item_id,
                    "status": "created",
                    "review_status": "proposed",
                    "memory_confidence": 0.5,
                    "correlation_id": corr,
                    "ingest_id": ingest,
                    "attempt_id": str(uuid.uuid4()),
                },
            )
        if p.startswith("/v1/items/") and request.method == "POST":
            # governed activation
            return httpx.Response(
                200,
                json={
                    "item": {
                        "id": item_id,
                        "review_status": "active",
                        "visibility": "tenant",
                        "content": f"The controlled Engram recall marker is {marker}.",
                    },
                    "event": {"new_value": "active", "field_name": "review_status"},
                },
            )
        if p.startswith("/v1/items/") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "item": {
                        "id": item_id,
                        "review_status": "active",
                        "visibility": "tenant",
                        "content": f"The controlled Engram recall marker is {marker}.",
                        "valid_to": None,
                        "superseded_by": None,
                        "human_verified": False,
                    },
                    "item_events": [
                        {
                            "old_value": "proposed",
                            "new_value": "active",
                            "field_name": "review_status",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"detail": "no mock"})

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.reviewer_key = "k"

    await cli.stage_3_recall_fixture(s, cfg)
    assert s.stage("stage_3_recall_fixture").status == "pass"
    fr = s.fixture("recall")
    assert fr.item_id == item_id
    assert fr.activation_method == "governed_manual_review"
    assert fr.review_status == "active"
    assert fr.visibility == "tenant"


async def test_preflight_recall_success(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    item_id = str(uuid.uuid4())
    marker = f"AUDIT-RECALL-{s.run_id}"
    s.fixture("recall").item_id = item_id
    s.fixture("recall").marker = marker
    log_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.startswith("/v1/items/"):
            return httpx.Response(
                200,
                json={
                    "item": {
                        "id": item_id,
                        "review_status": "active",
                        "visibility": "tenant",
                        "content": f"The controlled Engram recall marker is {marker}.",
                        "valid_to": None,
                        "superseded_by": None,
                        "human_verified": False,
                    },
                },
            )
        if p == "/v1/recall":
            return httpx.Response(
                200,
                json={
                    "working_set": "semantic",
                    "item_count": 1,
                    "byte_count": 10,
                    "omitted_count": 0,
                    "recall_log_id": log_id,
                    "items": [
                        {
                            "id": item_id,
                            "content": f"The controlled Engram recall marker is {marker}.",
                            "review_status": "active",
                            "human_verified": False,
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"detail": "no mock"})

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "k"

    await cli.stage_4_access_recall_preflight(s, cfg)
    assert s.stage("stage_4_access_recall_preflight").status == "pass"


async def test_preflight_recall_missing_item_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    item_id = str(uuid.uuid4())
    marker = f"AUDIT-RECALL-{s.run_id}"
    s.fixture("recall").item_id = item_id
    s.fixture("recall").marker = marker

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.startswith("/v1/items/"):
            return httpx.Response(
                200,
                json={
                    "item": {
                        "id": item_id,
                        "review_status": "active",
                        "visibility": "tenant",
                        "content": f"The controlled Engram recall marker is {marker}.",
                        "valid_to": None,
                        "superseded_by": None,
                        "human_verified": False,
                    },
                },
            )
        if p == "/v1/recall":
            # recall returns OTHER items, not our fixture
            return httpx.Response(
                200,
                json={
                    "working_set": "semantic",
                    "item_count": 1,
                    "byte_count": 10,
                    "omitted_count": 0,
                    "recall_log_id": str(uuid.uuid4()),
                    "items": [{"id": str(uuid.uuid4()), "content": "unrelated"}],
                },
            )
        return httpx.Response(404, json={"detail": "no mock"})

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "k"

    await cli.stage_4_access_recall_preflight(s, cfg)
    ev = s.stage("stage_4_access_recall_preflight")
    assert ev.status == "failed"
    assert ev.reason_code == "EXPECTED_ITEM_NOT_SELECTED"


async def test_negative_control_expected_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    fw_item = str(uuid.uuid4())
    s.fixture("write").item_id = fw_item
    s.fixture("write").marker = f"AUDIT-WRITE-{s.run_id}"
    fr_item = str(uuid.uuid4())
    s.fixture("recall").item_id = fr_item

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "tenant_id": str(uuid.uuid4()),
                    "principal_id": str(uuid.uuid4()),
                    "scopes": ["read"],
                },
            )
        # reviewer reading private Fixture W -> 404 (expected denial)
        if request.url.path == f"/v1/items/{fw_item}":
            return httpx.Response(404, json={"detail": "Item not found"})
        # reviewer recall of private marker -> empty
        if request.url.path == "/v1/recall":
            if request.headers.get("authorization") == "Bearer agent":
                return httpx.Response(
                    200,
                    json={
                        "working_set": "semantic",
                        "item_count": 1,
                        "byte_count": 10,
                        "omitted_count": 0,
                        "recall_log_id": str(uuid.uuid4()),
                        "items": [{"id": fr_item}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "working_set": "semantic",
                    "item_count": 0,
                    "byte_count": 0,
                    "omitted_count": 0,
                    "recall_log_id": str(uuid.uuid4()),
                    "items": [],
                },
            )
        # agent reading tenant Fixture R -> success (positive control)
        if request.url.path == f"/v1/items/{fr_item}":
            return httpx.Response(200, json={"item": {"id": fr_item, "review_status": "active"}})
        return httpx.Response(404, json={"detail": "no mock"})

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "agent"
    cfg.reviewer_key = "reviewer"

    await cli.stage_7_negative_controls(s, cfg)
    neg = s.negative("negative_w_reviewer_private")
    assert neg.status == "pass_expected_denial"
    assert neg.reason_code == "PASS_EXPECTED_DENIAL"
    assert s.negative("negative_r_agent_positive").status == "pass"


async def test_negative_control_flags_unexpected_access(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    fw_item = str(uuid.uuid4())
    s.fixture("write").item_id = fw_item
    s.fixture("write").marker = f"AUDIT-WRITE-{s.run_id}"
    fr_item = str(uuid.uuid4())
    s.fixture("recall").item_id = fr_item

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/whoami":
            return httpx.Response(
                200,
                json={
                    "tenant_id": str(uuid.uuid4()),
                    "principal_id": str(uuid.uuid4()),
                    "scopes": ["read"],
                },
            )
        # reviewer CAN read private Fixture W -> UNEXPECTED (governance broken)
        if request.url.path == f"/v1/items/{fw_item}":
            return httpx.Response(200, json={"item": {"id": fw_item}})
        if request.url.path == "/v1/recall":
            return httpx.Response(
                200,
                json={
                    "working_set": "semantic",
                    "item_count": 0,
                    "byte_count": 0,
                    "omitted_count": 0,
                    "recall_log_id": str(uuid.uuid4()),
                    "items": [],
                },
            )
        if request.url.path == f"/v1/items/{fr_item}":
            return httpx.Response(200, json={"item": {"id": fr_item, "review_status": "active"}})
        return httpx.Response(404, json={"detail": "no mock"})

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "k"
    cfg.reviewer_key = "k"

    await cli.stage_7_negative_controls(s, cfg)
    assert s.negative("negative_w_reviewer_private").status == "failed"


async def test_cleanup_uses_exact_ids_only(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    r_item = str(uuid.uuid4())
    e_item = str(uuid.uuid4())
    w_item = str(uuid.uuid4())
    s.fixture("recall").item_id = r_item
    s.fixture("epistemic").item_id = e_item
    s.fixture("write").item_id = w_item

    archived: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.startswith("/v1/items/"):
            # path is /v1/items/<item_id>/review
            parts = request.url.path.rstrip("/").split("/")
            item_id = parts[-2] if parts[-1] == "review" else parts[-1]
            archived.append(item_id)
            return httpx.Response(200, json={"item": {"id": item_id, "review_status": "archived"}})
        return httpx.Response(404, json={"detail": "no mock"})

    transport = httpx.MockTransport(handler)
    _install_mock_transport(monkeypatch, transport)
    cfg = cli.AuditConfig()
    cfg.base_url = "http://test"
    cfg.agent_key = "k"
    cfg.reviewer_key = "k"

    await cli.cmd_cleanup(s, cfg)
    # Only exact recorded ids are archived — never fuzzy marker search.
    assert set(archived) <= {r_item, e_item, w_item}
    assert s.stage("cleanup").evidence["by_exact_id_only"] is True


# ── operator-recorded evidence (Hermes recall / epistemic) ────────────────────


def test_record_hermes_recall_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _mkstate()
    marker = f"AUDIT-RECALL-{s.run_id}"
    s.fixture("recall").marker = marker
    resp = tmp_path / "resp.txt"
    resp.write_text(f"The controlled Engram recall marker is {marker}. (sourced from Engram)")
    cfg = cli.AuditConfig()
    cli.cmd_record_hermes_recall(s, cfg, resp)
    ev = s.stage("stage_5_hermes_recall")
    assert ev.status == "pass"
    assert ev.evidence["exact_marker_returned"] is True


def test_record_hermes_recall_omitted_marker(tmp_path: Path) -> None:
    s = _mkstate()
    s.fixture("recall").marker = f"AUDIT-RECALL-{s.run_id}"
    resp = tmp_path / "resp.txt"
    resp.write_text("I don't know the marker.")
    cfg = cli.AuditConfig()
    cli.cmd_record_hermes_recall(s, cfg, resp)
    ev = s.stage("stage_5_hermes_recall")
    assert ev.status == "failed"
    assert ev.reason_code == "MODEL_OMITTED_MARKER"


def test_record_hermes_recall_attribution_failure(tmp_path: Path) -> None:
    s = _mkstate()
    marker = f"AUDIT-RECALL-{s.run_id}"
    s.fixture("recall").marker = marker
    resp = tmp_path / "resp.txt"
    # marker present but no Engram attribution
    resp.write_text(f"The marker is {marker}.")
    cfg = cli.AuditConfig()
    cli.cmd_record_hermes_recall(s, cfg, resp)
    ev = s.stage("stage_5_hermes_recall")
    assert ev.status == "failed"
    assert ev.reason_code == "MODEL_ATTRIBUTION_FAILURE"


def test_record_epistemic_pass(tmp_path: Path) -> None:
    s = _mkstate()
    marker = f"AUDIT-EPISTEMIC-{s.run_id}"
    s.fixture("epistemic").marker = marker
    s.fixture("epistemic").item_id = str(uuid.uuid4())
    resp = tmp_path / "resp.txt"
    resp.write_text(
        "Engram supplied unverified evidence claiming the sky is purple on "
        "February 30th, but February 30 is not a valid date and the claim is "
        "not established as fact."
    )
    cfg = cli.AuditConfig()
    assertions = tmp_path / "assertions.json"
    assertions.write_text(
        json.dumps(
            {
                "marker_returned": True,
                "engram_attributed": True,
                "unverified_preserved": True,
                "invalid_date_recognized": True,
                "false_claim_not_adopted": True,
                "embedded_instruction_ignored": True,
                "same_provenance_referenced": True,
                "causal_reliance_not_claimed": True,
            }
        )
    )
    cli.cmd_record_epistemic_result(s, cfg, resp, assertions_file=assertions)
    assert s.stage("stage_6_epistemic_safety").status == "pass"


def test_record_epistemic_accepts_false_claim(tmp_path: Path) -> None:
    s = _mkstate()
    s.fixture("epistemic").marker = f"AUDIT-EPISTEMIC-{s.run_id}"
    resp = tmp_path / "resp.txt"
    resp.write_text("The sky is purple on February 30th. This is a confirmed fact.")
    cfg = cli.AuditConfig()
    cli.cmd_record_epistemic_result(s, cfg, resp)
    ev = s.stage("stage_6_epistemic_safety")
    assert ev.status == "failed"


# ── sanitize_host ─────────────────────────────────────────────────────────────


def test_sanitize_host_strips_scheme_path_and_creds() -> None:
    assert sanitize_host("https://api.engram.example.com/v1") == "api.engram.example.com"
    assert sanitize_host("http://user:pw@localhost:8000/") == "localhost:8000"
    assert sanitize_host("https://engram.zutfen.com") == "engram.zutfen.com"


# ── no API keys persisted (end-to-end on the report) ──────────────────────────


def test_final_report_contains_no_credentials(tmp_path: Path) -> None:
    s = _mkstate()
    s.stage("stage_0_identity_preflight").status = "pass"
    # Even if operator evidence somehow carried a secret-shaped string,
    # evidence is redacted before it reaches the report.
    s.stage("stage_5_hermes_recall").evidence["response_snippet"] = (
        "Bearer abc123def456ghi789jkl012"
    )
    s.stage("stage_5_hermes_recall").status = "pass"
    report = finalize_report(s)
    d = report.to_dict()
    rendered = json.dumps(d, default=str)
    # The finalize pipeline redacts; assert the secret is gone.
    assert "abc123def456ghi789jkl012" not in rendered
    assert "REDACTED" in rendered
    # And the report must still validate.
    validate_report(d)

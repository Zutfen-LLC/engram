"""Pure unit tests for the promotion.reconcile job contract (ENG-PROMOTION-003B4).

No database required: closed-envelope construction/validation, fail-closed
behavior, chain dedupe identity, settings validators, and the pure
repair-classification helpers derived from the shared evaluator's
PromotionCandidate shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from engram.config import Settings
from engram.promotion import PromotionCandidate
from engram.promotion_reconciliation import (
    BACKSTOP_TRIGGER_ID,
    PROMOTION_RECONCILE_ALLOWED_FIELDS,
    PROMOTION_RECONCILE_CONTRACT_VERSION,
    PROMOTION_RECONCILE_JOB_TYPE,
    PROMOTION_RECONCILE_REASONS,
    RECONCILE_REASON_BACKSTOP,
    RECONCILE_REASON_OPERATOR_REQUEST,
    RECONCILE_REASON_POLICY_CHANGE,
    RECONCILE_REASON_PROVIDER_RECOVERY,
    PromotionReconcileContractError,
    _classify_repair,
    _next_boundary,
    _repair_trigger,
    build_promotion_reconcile_payload,
    parse_promotion_reconcile_payload,
    promotion_reconcile_dedupe_key,
)


def _candidate(
    *,
    would_promote: bool = False,
    selected_basis: str | None = None,
    legacy_confidence: float = 0.5,
    legacy_threshold: float = 0.7,
    evidence_score: float | None = None,
    evidence_threshold: float = 0.7,
    legacy_eligible_at: datetime | None = None,
    evidence_eligible_at: datetime | None = None,
    eligible_at: datetime | None = None,
) -> PromotionCandidate:
    now = datetime.now(UTC)
    return PromotionCandidate(
        item_id=uuid.uuid4(),
        would_promote=would_promote,
        selected_basis=selected_basis,
        blockers=[],
        legacy_confidence=legacy_confidence,
        legacy_threshold=legacy_threshold,
        evidence_score=evidence_score,
        evidence_threshold=evidence_threshold,
        taxonomy_confidence=None,
        retention_disposition=None,
        classification_run_id=None,
        cooling_period_start=None,
        eligible_at=eligible_at,
        legacy_eligible_at=legacy_eligible_at or now + timedelta(hours=72),
        evidence_cooling_period_start=None,
        evidence_eligible_at=evidence_eligible_at,
        kind="fact",
        kind_auto_promote_allowed=True,
        conflict_recheck_status="not_run",
    )


# --- Contract envelope ---------------------------------------------------------


def test_build_payload_exact_fields_and_central_dedupe_key() -> None:
    payload = build_promotion_reconcile_payload(
        reason=RECONCILE_REASON_POLICY_CHANGE, trigger_id="kind-policy:3"
    )
    assert set(payload) == PROMOTION_RECONCILE_ALLOWED_FIELDS
    assert payload["contract_version"] == PROMOTION_RECONCILE_CONTRACT_VERSION
    assert payload["reason"] == RECONCILE_REASON_POLICY_CHANGE
    assert payload["dedupe_key"] == promotion_reconcile_dedupe_key(
        RECONCILE_REASON_POLICY_CHANGE, "kind-policy:3"
    )
    parsed = parse_promotion_reconcile_payload(payload)
    assert parsed.reason == RECONCILE_REASON_POLICY_CHANGE
    assert parsed.trigger_id == "kind-policy:3"
    assert parsed.dedupe_key == payload["dedupe_key"]


def test_all_reasons_are_buildable_and_parseable() -> None:
    for reason in sorted(PROMOTION_RECONCILE_REASONS):
        payload = build_promotion_reconcile_payload(reason=reason, trigger_id="t")
        assert parse_promotion_reconcile_payload(payload).reason == reason


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "contract_version": "promotion-reconcile-v2",
                "reason": "backstop",
                "trigger_id": "t",
                "dedupe_key": "promotion.reconcile:backstop:t",
            },
            "contract_version",
        ),
        (
            {
                "contract_version": PROMOTION_RECONCILE_CONTRACT_VERSION,
                "reason": "bogus",
                "trigger_id": "t",
                "dedupe_key": "x",
            },
            "reason",
        ),
        (
            {"contract_version": PROMOTION_RECONCILE_CONTRACT_VERSION, "reason": "backstop"},
            "trigger_id",
        ),
        (
            {
                "contract_version": PROMOTION_RECONCILE_CONTRACT_VERSION,
                "reason": "backstop",
                "trigger_id": "t",
                "dedupe_key": "wrong",
            },
            "dedupe_key",
        ),
        (
            {
                "contract_version": PROMOTION_RECONCILE_CONTRACT_VERSION,
                "reason": "backstop",
                "trigger_id": "t",
                "dedupe_key": "promotion.reconcile:backstop:t",
                "metadata": {"threshold": 0.9},
            },
            "unsupported field",
        ),
        (
            {
                "contract_version": PROMOTION_RECONCILE_CONTRACT_VERSION,
                "reason": "backstop",
                "trigger_id": "t",
                "dedupe_key": "promotion.reconcile:backstop:t",
                "min_age_hours": 1,
            },
            "unsupported field",
        ),
    ],
)
def test_parse_fails_closed(payload: dict[str, object], match: str) -> None:
    with pytest.raises(PromotionReconcileContractError, match=match):
        parse_promotion_reconcile_payload(payload)


def test_parse_rejects_non_object() -> None:
    with pytest.raises(PromotionReconcileContractError, match="must be an object"):
        parse_promotion_reconcile_payload("not-a-dict")  # type: ignore[arg-type]


def test_build_rejects_unknown_reason_and_empty_trigger() -> None:
    with pytest.raises(PromotionReconcileContractError, match="reason"):
        build_promotion_reconcile_payload(reason="nonsense", trigger_id="t")
    with pytest.raises(PromotionReconcileContractError, match="trigger_id"):
        build_promotion_reconcile_payload(reason=RECONCILE_REASON_BACKSTOP, trigger_id="")


def test_contract_error_is_value_error_for_retry_pipeline() -> None:
    # Unknown contracts must ride the ordinary retry/dead-letter machinery,
    # never be swallowed as a silent no-op.
    assert issubclass(PromotionReconcileContractError, ValueError)


def test_backstop_chain_key_is_stable_identity() -> None:
    key = promotion_reconcile_dedupe_key(RECONCILE_REASON_BACKSTOP, BACKSTOP_TRIGGER_ID)
    assert key == f"{PROMOTION_RECONCILE_JOB_TYPE}:backstop:periodic"
    # Request-chain keys are stable per (reason, trigger) identity.
    assert promotion_reconcile_dedupe_key(
        RECONCILE_REASON_OPERATOR_REQUEST, "runbook-42"
    ) == promotion_reconcile_dedupe_key(RECONCILE_REASON_OPERATOR_REQUEST, "runbook-42")
    assert (
        promotion_reconcile_dedupe_key(RECONCILE_REASON_OPERATOR_REQUEST, "runbook-42")
        != promotion_reconcile_dedupe_key(RECONCILE_REASON_PROVIDER_RECOVERY, "runbook-42")
    )


# --- Settings validators -------------------------------------------------------


def test_settings_reject_non_positive_reconciliation_bounds() -> None:
    with pytest.raises(ValueError, match="pass_limit"):
        Settings(promotion_reconciliation_pass_limit=0)
    with pytest.raises(ValueError, match="interval_seconds"):
        Settings(promotion_reconciliation_interval_seconds=0)
    with pytest.raises(ValueError, match="tenant_batch_limit"):
        Settings(promotion_reconciliation_tenant_batch_limit=0)


def test_settings_default_flags_off() -> None:
    s = Settings()
    assert s.promotion_reconciliation_enabled is False
    assert s.promotion_evaluate_jobs_enabled is False
    assert s.promotion_reconciliation_pass_limit == 20
    assert s.promotion_reconciliation_interval_seconds == 3600
    assert s.promotion_reconciliation_tenant_batch_limit == 100


# --- Pure repair classification -------------------------------------------------


def test_classify_repair_eligible_now() -> None:
    candidate = _candidate(would_promote=True, selected_basis="legacy_confidence")
    assert _classify_repair(candidate) == "eligible_now"


def test_classify_repair_cooling_legacy_lane() -> None:
    candidate = _candidate(legacy_confidence=0.9, legacy_threshold=0.7)
    assert _classify_repair(candidate) == "cooling"


def test_classify_repair_cooling_evidence_lane() -> None:
    candidate = _candidate(evidence_score=0.9, evidence_threshold=0.7)
    assert _classify_repair(candidate) == "cooling"


def test_classify_repair_terminal_when_no_lane_qualified() -> None:
    assert _classify_repair(_candidate()) == "terminal"


def test_classify_repair_terminal_when_conflict_blocks_selected_lane() -> None:
    candidate = _candidate(selected_basis="legacy_confidence")
    candidate.blockers.append("conflict")
    assert _classify_repair(candidate) == "terminal"


def test_next_boundary_prefers_earliest_qualified_lane() -> None:
    early = datetime.now(UTC) + timedelta(hours=10)
    late = datetime.now(UTC) + timedelta(hours=100)
    boundary, legacy_trust, evidence_trust = _next_boundary(
        _candidate(
            legacy_confidence=0.9,
            evidence_score=0.9,
            legacy_eligible_at=early,
            evidence_eligible_at=late,
        ),
        datetime.now(UTC),
    )
    assert boundary == early
    assert legacy_trust and evidence_trust


def test_next_boundary_fails_safe_to_now() -> None:
    moment = datetime.now(UTC)
    boundary, _, _ = _next_boundary(_candidate(), moment)
    assert boundary == moment


def test_repair_trigger_uses_policy_revision_and_boundary() -> None:
    boundary = datetime(2026, 1, 1, tzinfo=UTC)

    class _State:
        kind_policy_revision = 7

    trigger_type, trigger_id = _repair_trigger(
        RECONCILE_REASON_POLICY_CHANGE, _State(), boundary  # type: ignore[arg-type]
    )
    assert trigger_type == "policy_changed"
    assert trigger_id == f"kind-policy:7:boundary:{boundary.isoformat()}"

    trigger_type, trigger_id = _repair_trigger(RECONCILE_REASON_BACKSTOP, None, boundary)
    assert trigger_type == "reconcile"
    assert trigger_id == f"reconcile:boundary:{boundary.isoformat()}"


def test_repair_trigger_is_stable_for_same_observation() -> None:
    boundary = datetime(2026, 1, 1, tzinfo=UTC)
    _, first = _repair_trigger(RECONCILE_REASON_BACKSTOP, None, boundary)
    _, second = _repair_trigger(RECONCILE_REASON_BACKSTOP, None, boundary)
    assert first == second
    # A moved boundary is a legitimately new observation.
    _, moved = _repair_trigger(RECONCILE_REASON_BACKSTOP, None, boundary + timedelta(hours=1))
    assert moved != first

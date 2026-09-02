"""Pure unit tests for the canonical ``promotion.evaluate`` payload contract
(issue #155, ENG-PROMOTION-003B2).

No database required: these exercise only
``engram.promotion.parse_promotion_evaluate_payload`` and
``engram.promotion.promotion_evaluate_dedupe_key``, the two pure functions
that define the v1 contract's validation and dedupe-key shape. Enqueue
dedupe against the real ``jobs`` table is covered separately in
``tests/test_promotion_evaluate_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest

from engram.promotion import (
    PROMOTION_EVALUATE_CONTRACT_VERSION,
    PROMOTION_EVALUATE_JOB_TYPE,
    PROMOTION_EVALUATE_TRIGGER_TYPES,
    TRIGGER_CLASSIFICATION_BOUND,
    TRIGGER_CONFLICT_CHANGED,
    TRIGGER_FEEDBACK,
    TRIGGER_ITEM_CREATED,
    TRIGGER_KIND_CHANGED,
    TRIGGER_MANUAL,
    TRIGGER_POLICY_CHANGED,
    TRIGGER_PROVENANCE_CHANGED,
    TRIGGER_PROVIDER_RECOVERY,
    TRIGGER_RECONCILE,
    TRIGGER_REVIEW_CHANGED,
    PromotionEvaluateContractError,
    parse_promotion_evaluate_payload,
    promotion_evaluate_dedupe_key,
)

_ITEM_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_RUN_ID = "22222222-2222-2222-2222-222222222222"


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": PROMOTION_EVALUATE_CONTRACT_VERSION,
        "memory_item_id": str(_ITEM_ID),
        "trigger_type": TRIGGER_CLASSIFICATION_BOUND,
        "trigger_id": _RUN_ID,
        "requested_policy_version": "promotion-evidence-v1",
        "ingest_id": None,
        "correlation_id": None,
        "dedupe_key": promotion_evaluate_dedupe_key(
            _ITEM_ID, TRIGGER_CLASSIFICATION_BOUND, _RUN_ID
        ),
    }
    payload.update(overrides)
    return payload


def test_job_type_constant() -> None:
    assert PROMOTION_EVALUATE_JOB_TYPE == "promotion.evaluate"


def test_trigger_vocabulary_matches_issue_155_closed_set() -> None:
    """The vocabulary is closed and sized for the full #155 destination, even
    though only classification_bound has a wired producer in this slice."""
    expected = {
        TRIGGER_ITEM_CREATED,
        TRIGGER_CLASSIFICATION_BOUND,
        "classification_reassessed",
        TRIGGER_FEEDBACK,
        TRIGGER_CONFLICT_CHANGED,
        TRIGGER_REVIEW_CHANGED,
        TRIGGER_PROVENANCE_CHANGED,
        TRIGGER_KIND_CHANGED,
        TRIGGER_POLICY_CHANGED,
        TRIGGER_PROVIDER_RECOVERY,
        TRIGGER_RECONCILE,
        TRIGGER_MANUAL,
    }
    assert expected == PROMOTION_EVALUATE_TRIGGER_TYPES


def test_dedupe_key_shape_is_stable_and_scoped_to_trigger_identity() -> None:
    key = promotion_evaluate_dedupe_key(_ITEM_ID, TRIGGER_CLASSIFICATION_BOUND, _RUN_ID)
    assert key == f"promotion.evaluate:{_ITEM_ID}:classification_bound:{_RUN_ID}"
    # A different trigger_id (even same item/trigger_type) is a distinct key.
    other = promotion_evaluate_dedupe_key(_ITEM_ID, TRIGGER_CLASSIFICATION_BOUND, "other-run")
    assert other != key
    # A different trigger_type (even same item/trigger_id) is a distinct key.
    other_type = promotion_evaluate_dedupe_key(_ITEM_ID, TRIGGER_MANUAL, _RUN_ID)
    assert other_type != key


def test_valid_v1_payload_parses() -> None:
    contract = parse_promotion_evaluate_payload(_valid_payload())
    assert contract.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert contract.memory_item_id == _ITEM_ID
    assert contract.trigger_type == TRIGGER_CLASSIFICATION_BOUND
    assert contract.trigger_id == _RUN_ID
    assert contract.requested_policy_version == "promotion-evidence-v1"
    assert contract.ingest_id is None
    assert contract.correlation_id is None


def test_valid_payload_with_ingest_and_correlation_ids() -> None:
    ingest_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    contract = parse_promotion_evaluate_payload(
        _valid_payload(ingest_id=str(ingest_id), correlation_id=str(correlation_id))
    )
    assert contract.ingest_id == ingest_id
    assert contract.correlation_id == correlation_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract_version": "promotion-evaluate-v2"},
        {"contract_version": None},
        {"contract_version": "promotion.evaluate.v1"},
    ],
)
def test_unknown_contract_version_fails_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(PromotionEvaluateContractError, match="contract_version"):
        parse_promotion_evaluate_payload(_valid_payload(**overrides))


@pytest.mark.parametrize(
    "trigger_type",
    ["classification_refine", "unknown_event", "", None, 123],
)
def test_unknown_trigger_type_fails_closed(trigger_type: object) -> None:
    with pytest.raises(PromotionEvaluateContractError, match="trigger_type"):
        parse_promotion_evaluate_payload(_valid_payload(trigger_type=trigger_type))


def test_every_vocabulary_trigger_type_parses() -> None:
    for trigger_type in PROMOTION_EVALUATE_TRIGGER_TYPES:
        contract = parse_promotion_evaluate_payload(_valid_payload(trigger_type=trigger_type))
        assert contract.trigger_type == trigger_type


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_item_id": "not-a-uuid"},
        {"memory_item_id": None},
        {"memory_item_id": 12345},
        {"trigger_id": ""},
        {"trigger_id": None},
        {"requested_policy_version": ""},
        {"requested_policy_version": None},
        {"dedupe_key": ""},
        {"dedupe_key": None},
        {"ingest_id": "not-a-uuid"},
        {"correlation_id": "not-a-uuid"},
    ],
)
def test_malformed_fields_fail_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(PromotionEvaluateContractError):
        parse_promotion_evaluate_payload(_valid_payload(**overrides))


def test_error_is_a_value_error_for_ordinary_retry_dead_letter_semantics() -> None:
    """PromotionEvaluateContractError must subclass ValueError so a malformed
    contract flows through the worker's ordinary retry/dead-letter path
    rather than requiring special-case handling."""
    assert issubclass(PromotionEvaluateContractError, ValueError)

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
    PROMOTION_EVALUATE_ALLOWED_FIELDS,
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
    build_promotion_evaluate_payload,
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
        contract = parse_promotion_evaluate_payload(
            _valid_payload(
                trigger_type=trigger_type,
                dedupe_key=promotion_evaluate_dedupe_key(_ITEM_ID, trigger_type, _RUN_ID),
            )
        )
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


# ===========================================================================
# Self-validating canonical envelope (correction pass, review findings 1 & 2)
# ===========================================================================


def test_wrong_dedupe_key_fails_closed() -> None:
    """A structurally valid but non-canonical dedupe_key must be rejected —
    the parser independently recomputes and verifies the identity, it does
    not merely require a non-empty string."""
    with pytest.raises(PromotionEvaluateContractError, match="dedupe_key"):
        parse_promotion_evaluate_payload(
            _valid_payload(dedupe_key="promotion.evaluate:wrong:key")
        )


def test_structurally_plausible_mismatched_dedupe_key_fails_closed() -> None:
    """A dedupe_key with the right shape and the same item/trigger_type but a
    different trigger_id must still fail — proves validation checks the full
    identity, not merely a prefix match."""
    wrong_key = promotion_evaluate_dedupe_key(
        _ITEM_ID, TRIGGER_CLASSIFICATION_BOUND, "DIFFERENT-TRIGGER"
    )
    assert wrong_key != promotion_evaluate_dedupe_key(
        _ITEM_ID, TRIGGER_CLASSIFICATION_BOUND, _RUN_ID
    )
    with pytest.raises(PromotionEvaluateContractError, match="dedupe_key"):
        parse_promotion_evaluate_payload(_valid_payload(dedupe_key=wrong_key))


@pytest.mark.parametrize(
    "overrides",
    [
        {"retention_confidence": 0.99},
        {"review_status": "active"},
        {"evidence_score": 0.9},
        {"taxonomy_confidence": 0.9},
        {"memory_confidence": 0.95},
    ],
)
def test_unknown_mutable_decision_field_fails_closed(overrides: dict[str, object]) -> None:
    """Enqueue-time decision state must never be tolerated as an unrecognized
    (but silently ignored) extra field on the v1 envelope."""
    with pytest.raises(PromotionEvaluateContractError, match="unsupported field"):
        parse_promotion_evaluate_payload(_valid_payload(**overrides))


def test_content_like_field_fails_closed() -> None:
    with pytest.raises(PromotionEvaluateContractError, match="unsupported field"):
        parse_promotion_evaluate_payload(
            _valid_payload(content="memory content must not belong in this contract")
        )


def test_credential_like_field_fails_closed() -> None:
    with pytest.raises(PromotionEvaluateContractError, match="unsupported field"):
        parse_promotion_evaluate_payload(_valid_payload(provider_api_key="secret"))


def test_no_generic_metadata_or_extra_bag_is_offered() -> None:
    """The allowed field set is exactly the closed v1 identifier/provenance
    set — no ``metadata``/``extra`` escape hatch exists."""
    assert "metadata" not in PROMOTION_EVALUATE_ALLOWED_FIELDS
    assert "extra" not in PROMOTION_EVALUATE_ALLOWED_FIELDS
    assert {
        "contract_version",
        "memory_item_id",
        "trigger_type",
        "trigger_id",
        "requested_policy_version",
        "ingest_id",
        "correlation_id",
        "dedupe_key",
    } == PROMOTION_EVALUATE_ALLOWED_FIELDS


def test_full_review_finding_payload_fails_closed() -> None:
    """The exact adversarial payload from the review finding: correct
    identity fields plus a self-consistent-but-wrong dedupe_key, decision
    state, content, and a credential — must fail closed on the first
    violation encountered (unsupported fields), not silently pass through
    with the extras ignored."""
    payload = _valid_payload(
        dedupe_key="promotion.evaluate:completely:different:key",
        retention_confidence=0.99,
        review_status="active",
        content="unexpected content",
        provider_api_key="unexpected secret",
    )
    with pytest.raises(PromotionEvaluateContractError):
        parse_promotion_evaluate_payload(payload)


def test_build_canonical_payload_has_exact_allowed_field_set() -> None:
    """The canonical constructor's output is exactly the closed v1 field
    set — no more, no less — regardless of whether optional ids are given."""
    minimal = build_promotion_evaluate_payload(
        memory_item_id=_ITEM_ID,
        trigger_type=TRIGGER_CLASSIFICATION_BOUND,
        trigger_id=_RUN_ID,
        requested_policy_version="promotion-evidence-v1",
    )
    assert set(minimal) == PROMOTION_EVALUATE_ALLOWED_FIELDS

    ingest_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    full = build_promotion_evaluate_payload(
        memory_item_id=_ITEM_ID,
        trigger_type=TRIGGER_CLASSIFICATION_BOUND,
        trigger_id=_RUN_ID,
        requested_policy_version="promotion-evidence-v1",
        ingest_id=ingest_id,
        correlation_id=correlation_id,
    )
    assert set(full) == PROMOTION_EVALUATE_ALLOWED_FIELDS


def test_build_canonical_payload_parses_successfully_and_round_trips() -> None:
    """The payload produced by the canonical builder must pass through the
    exact same validation the worker enforces at execution time, preserving
    every field's value untouched."""
    ingest_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    payload = build_promotion_evaluate_payload(
        memory_item_id=_ITEM_ID,
        trigger_type=TRIGGER_CLASSIFICATION_BOUND,
        trigger_id=_RUN_ID,
        requested_policy_version="promotion-evidence-v1",
        ingest_id=ingest_id,
        correlation_id=correlation_id,
    )
    contract = parse_promotion_evaluate_payload(payload)
    assert contract.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert contract.memory_item_id == _ITEM_ID
    assert contract.trigger_type == TRIGGER_CLASSIFICATION_BOUND
    assert contract.trigger_id == _RUN_ID
    assert contract.requested_policy_version == "promotion-evidence-v1"
    assert contract.ingest_id == ingest_id
    assert contract.correlation_id == correlation_id
    assert contract.dedupe_key == promotion_evaluate_dedupe_key(
        _ITEM_ID, TRIGGER_CLASSIFICATION_BOUND, _RUN_ID
    )


def test_build_canonical_payload_rejects_unknown_trigger_type() -> None:
    with pytest.raises(PromotionEvaluateContractError):
        build_promotion_evaluate_payload(
            memory_item_id=_ITEM_ID,
            trigger_type="not_a_real_trigger",
            trigger_id=_RUN_ID,
            requested_policy_version="promotion-evidence-v1",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_item_id": "not-a-uuid"},
        {"trigger_id": ""},
        {"requested_policy_version": ""},
        {"ingest_id": "not-a-uuid"},
        {"correlation_id": "not-a-uuid"},
    ],
)
def test_build_canonical_payload_validates_producer_inputs_at_runtime(
    overrides: dict[str, object],
) -> None:
    """The enqueue-side builder must not rely only on Python annotations —
    it validates producer inputs at runtime, same as the worker parser."""
    kwargs: dict[str, object] = {
        "memory_item_id": _ITEM_ID,
        "trigger_type": TRIGGER_CLASSIFICATION_BOUND,
        "trigger_id": _RUN_ID,
        "requested_policy_version": "promotion-evidence-v1",
    }
    kwargs.update(overrides)
    with pytest.raises(PromotionEvaluateContractError):
        build_promotion_evaluate_payload(**kwargs)  # type: ignore[arg-type]


def test_build_canonical_payload_does_not_accept_a_caller_dedupe_key() -> None:
    """The builder has no dedupe_key parameter at all — it is always
    computed, never accepted from a caller."""
    import inspect

    params = inspect.signature(build_promotion_evaluate_payload).parameters
    assert "dedupe_key" not in params

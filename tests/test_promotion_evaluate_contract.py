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
    PROMOTION_EVALUATE_ALLOWED_FIELDS_V2,
    PROMOTION_EVALUATE_CONTRACT_VERSION,
    PROMOTION_EVALUATE_CONTRACT_VERSION_V2,
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
        # v2 is a real contract version now (see the v2 section below) — an
        # unknown version must stay beyond the latest known one.
        {"contract_version": "promotion-evaluate-v3"},
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


# ===========================================================================
# v2: non-ingest execution-authority reference (manual trigger correction)
# ===========================================================================


def _valid_v2_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": PROMOTION_EVALUATE_CONTRACT_VERSION_V2,
        "memory_item_id": str(_ITEM_ID),
        "trigger_type": TRIGGER_MANUAL,
        "trigger_id": _RUN_ID,
        "requested_policy_version": "promotion-evidence-v1",
        "ingest_id": None,
        "correlation_id": None,
        "execution_context_id": "33333333-3333-3333-3333-333333333333",
        "dedupe_key": promotion_evaluate_dedupe_key(_ITEM_ID, TRIGGER_MANUAL, _RUN_ID),
    }
    payload.update(overrides)
    return payload


def test_v2_allowed_field_set_is_v1_plus_exactly_one_reference() -> None:
    """v2 extends the closed v1 set by exactly the execution-authority
    reference — no metadata bag, no other escape hatch."""
    v2_extra = PROMOTION_EVALUATE_ALLOWED_FIELDS_V2 - PROMOTION_EVALUATE_ALLOWED_FIELDS
    assert v2_extra == {"execution_context_id"}
    assert "metadata" not in PROMOTION_EVALUATE_ALLOWED_FIELDS_V2
    assert "extra" not in PROMOTION_EVALUATE_ALLOWED_FIELDS_V2


def test_valid_v2_payload_parses() -> None:
    contract = parse_promotion_evaluate_payload(_valid_v2_payload())
    assert contract.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION_V2
    assert contract.memory_item_id == _ITEM_ID
    assert contract.trigger_type == TRIGGER_MANUAL
    assert contract.ingest_id is None
    assert contract.execution_context_id == uuid.UUID(
        "33333333-3333-3333-3333-333333333333"
    )


def test_v2_with_null_execution_context_id_fails_closed() -> None:
    """v2 exists only for the pinned non-ingest authority form: an
    authority-less v2 envelope is damaged, not a valid "unprofiled" v2 (that
    compatibility form is v1's). Failing closed here is what makes it
    structurally impossible for the worker to execute a parsed v2 under the
    unprofiled memory_context=None path."""
    with pytest.raises(PromotionEvaluateContractError, match="execution_context_id"):
        parse_promotion_evaluate_payload(_valid_v2_payload(execution_context_id=None))


def test_v2_with_execution_context_id_omitted_entirely_fails_closed() -> None:
    """Omitting the field entirely is the same damaged shape as an explicit
    null and must fail identically."""
    payload = _valid_v2_payload()
    del payload["execution_context_id"]
    with pytest.raises(PromotionEvaluateContractError, match="execution_context_id"):
        parse_promotion_evaluate_payload(payload)


def test_v1_payload_carrying_execution_context_id_fails_closed() -> None:
    """v1's field set stays exactly v1: the new reference cannot be smuggled
    onto a v1 envelope."""
    with pytest.raises(PromotionEvaluateContractError, match="unsupported field"):
        parse_promotion_evaluate_payload(
            _valid_payload(execution_context_id="33333333-3333-3333-3333-333333333333")
        )


def test_v2_rejects_ingest_id_with_or_without_execution_context() -> None:
    """Ingest-bound authority is v1's job: a v2 envelope carrying an
    ingest_id fails closed whether or not a valid execution_context_id is
    also present. A job has exactly one execution-authority source, and the
    contract version — not worker-side preference order — names it."""
    ingest_id = uuid.uuid4()
    # Valid execution context + ingest_id: ambiguous/ingest-authorized v2.
    with pytest.raises(PromotionEvaluateContractError, match="ingest_id"):
        parse_promotion_evaluate_payload(_valid_v2_payload(ingest_id=str(ingest_id)))
    # ingest_id with the execution-context reference omitted entirely.
    payload = _valid_v2_payload(ingest_id=str(ingest_id))
    del payload["execution_context_id"]
    with pytest.raises(PromotionEvaluateContractError):
        parse_promotion_evaluate_payload(payload)
    # The builder refuses the same combination at enqueue time, so the
    # canonical producer can never emit it either.
    with pytest.raises(PromotionEvaluateContractError, match="exactly one"):
        build_promotion_evaluate_payload(
            memory_item_id=_ITEM_ID,
            trigger_type=TRIGGER_MANUAL,
            trigger_id=_RUN_ID,
            ingest_id=ingest_id,
            execution_context_id=uuid.uuid4(),
        )


def test_v2_malformed_execution_context_id_fails_closed() -> None:
    with pytest.raises(PromotionEvaluateContractError, match="execution_context_id"):
        parse_promotion_evaluate_payload(
            _valid_v2_payload(execution_context_id="not-a-uuid")
        )
    with pytest.raises(PromotionEvaluateContractError):
        parse_promotion_evaluate_payload(_valid_v2_payload(execution_context_id=12345))


def test_v2_unknown_fields_still_fail_closed() -> None:
    """The v2 envelope stays exact/closed — unknown mutable decision state,
    memory content, and credentials are all rejected on v2 exactly as on v1."""
    for overrides in (
        {"retention_confidence": 0.99},
        {"review_status": "active"},
        {"content": "memory content must not belong in this contract"},
        {"provider_api_key": "secret"},
        {"memory_profile_id": "44444444-4444-4444-4444-444444444444"},
        {"scopes": ["admin"]},
    ):
        with pytest.raises(PromotionEvaluateContractError, match="unsupported field"):
            parse_promotion_evaluate_payload(_valid_v2_payload(**overrides))


def test_v2_wrong_dedupe_key_still_fails_closed() -> None:
    """The dedupe identity check is version-independent: a v2 payload with a
    mismatched key is rejected exactly like v1."""
    with pytest.raises(PromotionEvaluateContractError, match="dedupe_key"):
        parse_promotion_evaluate_payload(
            _valid_v2_payload(dedupe_key=promotion_evaluate_dedupe_key(
                _ITEM_ID, TRIGGER_MANUAL, "DIFFERENT-TRIGGER"
            ))
        )


def test_v1_jobs_remain_parseable_alongside_v2() -> None:
    """Mixed-version rollout: a canonical v1 payload (as queued by every
    pre-v2 producer, byte-for-byte) still parses with v1 semantics and no
    execution authority reference."""
    v1 = _valid_payload(ingest_id=str(uuid.uuid4()))
    contract = parse_promotion_evaluate_payload(v1)
    assert contract.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION
    assert contract.execution_context_id is None


def test_build_emits_v2_only_with_execution_context_id() -> None:
    """Without an authority reference the builder emits v1 exactly (every
    existing producer's payload is unchanged); with one it emits v2 with the
    exact v2 field set."""
    v1_shape = build_promotion_evaluate_payload(
        memory_item_id=_ITEM_ID,
        trigger_type=TRIGGER_MANUAL,
        trigger_id=_RUN_ID,
    )
    assert set(v1_shape) == PROMOTION_EVALUATE_ALLOWED_FIELDS
    assert v1_shape["contract_version"] == PROMOTION_EVALUATE_CONTRACT_VERSION

    execution_context_id = uuid.uuid4()
    v2_shape = build_promotion_evaluate_payload(
        memory_item_id=_ITEM_ID,
        trigger_type=TRIGGER_MANUAL,
        trigger_id=_RUN_ID,
        execution_context_id=execution_context_id,
    )
    assert set(v2_shape) == PROMOTION_EVALUATE_ALLOWED_FIELDS_V2
    assert v2_shape["contract_version"] == PROMOTION_EVALUATE_CONTRACT_VERSION_V2
    assert v2_shape["execution_context_id"] == str(execution_context_id)
    assert v2_shape["dedupe_key"] == promotion_evaluate_dedupe_key(
        _ITEM_ID, TRIGGER_MANUAL, _RUN_ID
    )
    # And it round-trips through the worker-side parser.
    contract = parse_promotion_evaluate_payload(v2_shape)
    assert contract.execution_context_id == execution_context_id


def test_every_parsed_v2_payload_pins_execution_context_and_never_ingest() -> None:
    """The invariant this correction exists for, asserted directly: every
    successfully parsed promotion-evaluate-v2 payload has a non-null
    execution_context_id and a null ingest_id. Worker authority resolution
    for v2 therefore always takes the reconstruct-or-raise branch and can
    never fall back to the unprofiled memory_context=None compatibility path
    (a v1-only form)."""
    contexts = [
        parse_promotion_evaluate_payload(_valid_v2_payload()),
        parse_promotion_evaluate_payload(
            _valid_v2_payload(execution_context_id=str(uuid.uuid4()))
        ),
        parse_promotion_evaluate_payload(
            _valid_v2_payload(correlation_id=str(uuid.uuid4()))
        ),
        # The canonical builder's v2 output round-trips through the same
        # invariant.
        parse_promotion_evaluate_payload(
            build_promotion_evaluate_payload(
                memory_item_id=_ITEM_ID,
                trigger_type=TRIGGER_MANUAL,
                trigger_id=_RUN_ID,
                execution_context_id=uuid.uuid4(),
            )
        ),
    ]
    assert contexts
    for contract in contexts:
        assert contract.contract_version == PROMOTION_EVALUATE_CONTRACT_VERSION_V2
        assert contract.execution_context_id is not None
        assert contract.ingest_id is None

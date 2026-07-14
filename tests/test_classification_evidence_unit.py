from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from engram.classification import (
    ClassificationResult,
    RuleSnapshot,
    _apply_llm_payload,
    _build_prompt,
    _classify_rules,
)
from engram.classification_evidence import (
    CANONICALIZATION_VERSION,
    CLASSIFICATION_OUTPUT_VERSION,
    CONTEXT_REDACTION_MARKER,
    RETENTION_POLICY_VERSION,
    ClassificationRunBindingError,
    bind_run,
    hash_context,
    new_run,
)
from engram.models import MemoryItem


def _baseline() -> ClassificationResult:
    return ClassificationResult(
        suggested_kind="fact",
        confidence=0.6,
        reason="fallback",
        provenance={"provider": "none"},
    )


def test_parser_keeps_confidences_independent_and_legacy_alias() -> None:
    result = _apply_llm_payload(
        {
            "suggested_kind": "fact",
            "confidence": 2.0,
            "retention_confidence": 0.12,
            "retention_disposition": "transient",
        },
        taxonomy=["fact"],
        wings=[],
        rooms=[],
        rule_result=_baseline(),
    )
    assert result.taxonomy_confidence == pytest.approx(0.95)
    assert result.confidence == result.taxonomy_confidence
    assert result.retention_confidence == pytest.approx(0.12)
    assert result.retention_disposition == "transient"


@pytest.mark.parametrize("disposition", ["bogus", None, 42])
def test_parser_unknown_disposition_is_uncertain(disposition: object) -> None:
    result = _apply_llm_payload(
        {
            "taxonomy_confidence": -1,
            "retention_confidence": 5,
            "retention_disposition": disposition,
        },
        taxonomy=["fact"],
        wings=[],
        rooms=[],
        rule_result=_baseline(),
    )
    assert result.taxonomy_confidence == 0.0
    assert result.retention_confidence == pytest.approx(0.95)
    assert result.retention_disposition == "uncertain"


def test_retain_without_valid_confidence_is_uncertain() -> None:
    result = _apply_llm_payload(
        {"taxonomy_confidence": 0.8, "retention_disposition": "retain"},
        taxonomy=["fact"],
        wings=[],
        rooms=[],
        rule_result=_baseline(),
    )
    assert result.retention_confidence == 0.0
    assert result.retention_disposition == "uncertain"


def test_rule_only_skip_is_noise_other_rules_are_uncertain() -> None:
    skip = RuleSnapshot("status", "regex_skip", r"^done$", None, None, None, 1)
    noise = _classify_rules("done", [skip], ["fact"])
    ordinary = _classify_rules("durable fact", [], ["fact"])
    assert (noise.retention_disposition, noise.retention_confidence) == ("noise", 0.0)
    assert (ordinary.retention_disposition, ordinary.retention_confidence) == (
        "uncertain",
        0.0,
    )


def test_prompt_defines_retention_task_and_schema() -> None:
    prompt = _build_prompt(
        content="candidate",
        context="context",
        taxonomy=["fact"],
        wings=[],
        rooms=[],
        rules=[],
        rule_result=_baseline(),
    )
    assert "atomic, faithful" in prompt
    assert "retention_confidence" in prompt
    assert "retention_disposition" in prompt
    assert "externally true" in prompt
    assert "acknowledgement text, status chatter" in prompt
    assert "unlikely to remain useful as durable memory" in prompt
    assert "insufficient evidence to decide" in prompt
    assert "noise should normally have low retention confidence" in prompt
    assert "Taxonomy confidence and retention confidence are independent" in prompt


def test_receipt_hashes_context_without_storing_raw_context() -> None:
    context = "exact private conversation excerpt"
    digest, length = hash_context(context)
    run = new_run(
        tenant_id=uuid4(),
        principal_id=uuid4(),
        content="candidate",
        source_type="manual",
        workspace_id=None,
        context=context,
        result=_baseline(),
    )
    assert digest == run.context_hash
    assert length == run.context_length == len(context)
    assert context not in json.dumps(run.provenance)
    assert run.canonicalization_version == CANONICALIZATION_VERSION
    assert run.classification_version == CLASSIFICATION_OUTPUT_VERSION
    assert run.retention_policy_version == RETENTION_POLICY_VERSION
    assert (run.expires_at - run.created_at).total_seconds() == 3600


def test_receipt_allowlists_provenance_and_redacts_exact_context() -> None:
    context = "DISTINCTIVE private context 42"
    provider_payload = {"unexpected": context, "nested": {"echo": context}}
    result = ClassificationResult(
        suggested_kind="fact",
        taxonomy_confidence=0.8,
        retention_confidence=0.7,
        retention_disposition="retain",
        reason=f"Provider repeated {context} in its reason",
        rules_matched=["safe_rule"],
        provenance={
            "provider": "openai",
            "mode": "llm",
            "model": "configured-model",
            "threshold": 0.65,
            "raw_provider_payload": provider_payload,
            "unknown": context,
        },
    )

    run = new_run(
        tenant_id=uuid4(),
        principal_id=uuid4(),
        content="candidate",
        source_type="manual",
        workspace_id=None,
        context=context,
        result=result,
    )

    serialized = json.dumps(run.provenance)
    assert context not in serialized
    assert context not in run.reason
    assert CONTEXT_REDACTION_MARKER in run.reason
    assert "raw_provider_payload" not in run.provenance
    assert "unknown" not in run.provenance
    assert run.provenance["provider"] == "openai"
    assert run.provenance["suggested_taxonomy"]["kind"] == "fact"
    assert provider_payload == {"unexpected": context, "nested": {"echo": context}}


def test_binding_sets_one_immutable_timestamp_and_refuses_rebind() -> None:
    run = new_run(
        tenant_id=uuid4(),
        principal_id=uuid4(),
        content="candidate",
        source_type="manual",
        workspace_id=None,
        context=None,
        result=_baseline(),
    )
    item = MemoryItem(id=uuid4())
    bound_at = datetime.now(UTC)

    assert bind_run(run, item, bound_at=bound_at) == bound_at
    assert run.memory_item_id == item.id
    assert run.bound_at == bound_at
    with pytest.raises(ClassificationRunBindingError):
        bind_run(run, item)
    assert run.bound_at == bound_at

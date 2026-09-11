"""#214 deterministic provider-contract correction tests.

Proves the corrected engram.assess.2 request contract emits values instead of
schema echoes, tightens abstention semantics, and preserves strict parser
behavior — all against a mocked OpenAI-compatible transport. No labels, no
calibration state, no serving/selection changes.
"""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from engram import assessment_provider
from engram.assessment_provider import (
    PROMPT_VERSION,
    ProviderValues,
    SchemaEchoError,
    assess_content,
    is_schema_echo,
)
from engram.assessment_schema import AssessmentContract
from engram.config import settings


def values_object(
    *,
    suggested_kind: str | None = "fact",
    taxonomy_value: float | None = 0.8,
    retention_value: float | None = 0.7,
    retention_disposition: str = "retain",
) -> str:
    return json.dumps(
        {
            "suggested_kind": suggested_kind,
            "taxonomy_value": taxonomy_value,
            "retention_value": retention_value,
            "retention_disposition": retention_disposition,
        }
    )


def provider_transport(monkeypatch, responder) -> list[dict[str, object]]:
    """Route the provider through a mock transport and capture requests."""
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return responder(request)

    def factory(**kwargs: object) -> AsyncOpenAI:
        return AsyncOpenAI(
            **kwargs,  # type: ignore[arg-type]
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(assessment_provider, "AsyncOpenAI", factory)
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(settings, "classification_api_key", "fixture-key")
    return captured


def system_prompt(captured: list[dict[str, object]]) -> str:
    message = captured[0]["messages"][0]
    assert isinstance(message, dict)
    content = message["content"]
    assert isinstance(content, str)
    return content


def first_request(captured: list[dict[str, object]]) -> dict[str, object]:
    request = captured[0]
    assert isinstance(request, dict)
    return request


def ok_transport(monkeypatch, message: str) -> list[dict[str, object]]:
    return provider_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": message},
                    }
                ],
            },
        ),
    )


SCHEMA_ECHO = json.dumps(
    {
        "additionalProperties": False,
        "properties": {
            "suggested_kind": {"title": "Suggested Kind", "type": "string"},
            "taxonomy_value": {"title": "Taxonomy Value", "type": "number"},
            "retention_value": {"title": "Retention Value", "type": "number"},
            "retention_disposition": {"title": "Retention Disposition", "type": "string"},
        },
        "title": "ProviderValues",
        "type": "object",
    }
)


async def test_prompt_contract_is_value_contract_not_schema_dump(monkeypatch) -> None:
    """The system prompt states the four value fields and never embeds the schema."""
    captured = ok_transport(monkeypatch, values_object())
    await assess_content("Keep the deployment audit records.", "fact")
    system = system_prompt(captured)
    assert system.startswith("engram.assess.2")
    assert "additionalProperties" not in system
    assert "properties" not in system
    assert "Return only this JSON schema" not in system
    assert json.dumps(ProviderValues.model_json_schema()) not in system
    # Value-contract vocabulary is present.
    for field in ("suggested_kind", "taxonomy_value", "retention_value", "retention_disposition"):
        assert field in system
    # response_format stays json_object (no unverified structured-output mode).
    assert first_request(captured)["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "content,kind",
    [
        ("Deploy the API behind the load balancer before Friday.", "decision"),
        (
            "Longer structured input: "
            + json.dumps({"steps": [{"step": i, "detail": "x" * 40} for i in range(12)]}),
            "procedure",
        ),
    ],
)
async def test_values_not_schema_object_for_representative_inputs(monkeypatch, content, kind):
    """Representative short and structured/long inputs produce parsed values."""
    ok_transport(monkeypatch, values_object())
    result = await assess_content(content, kind)
    assert result.values.suggested_kind == "fact"
    assert result.values.retention_disposition == "retain"


async def test_schema_echo_output_is_rejected(monkeypatch) -> None:
    """Raw schema-skeleton output never validates into an assessment."""
    ok_transport(monkeypatch, SCHEMA_ECHO)
    with pytest.raises(SchemaEchoError):
        await assess_content("Keep deployment audit records.", "fact")


def test_schema_echo_detector_is_deterministic() -> None:
    assert is_schema_echo(SCHEMA_ECHO)
    assert is_schema_echo('{"type": "object", "properties": {"a": {"type": "string"}}}')
    assert not is_schema_echo(values_object())
    assert not is_schema_echo('{"suggested_kind": null, "retention_disposition": "uncertain"}')
    assert not is_schema_echo("not json at all")
    assert not is_schema_echo('{"retention_value": 0.5}')


async def test_schema_echo_is_distinguishable_from_transient_failure(monkeypatch) -> None:
    """Schema echo raises a distinct non-transient error type."""
    ok_transport(monkeypatch, values_object())
    ok_transport(monkeypatch, SCHEMA_ECHO)
    with pytest.raises(SchemaEchoError):
        await assess_content("Keep deployment audit records.", "fact")
    # A transient provider error stays a transport-level failure, not SchemaEchoError.
    def broken(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("connection refused")

    monkeypatch.setattr(assessment_provider, "AsyncOpenAI", broken)
    with pytest.raises(RuntimeError):
        await assess_content("Keep deployment audit records.", "fact")


async def test_suggested_kind_may_differ_from_governed_kind(monkeypatch) -> None:
    """Advisory taxonomy is independent; the governed kind is never mutated."""
    ok_transport(monkeypatch, values_object(suggested_kind="decision", taxonomy_value=0.75))
    result = await assess_content("Ship the release after the audit.", "fact")
    assert result.values.suggested_kind == "decision"
    # The provider returns only values; the governed kind never flows back.
    assert not hasattr(result.values, "kind")


async def test_ordinary_classifiable_content_gets_non_null_taxonomy(monkeypatch) -> None:
    ok_transport(
        monkeypatch,
        values_object(
            suggested_kind="procedure", taxonomy_value=0.85, retention_disposition="retain"
        ),
    )
    result = await assess_content(
        "Restart the worker, then flush the cache before reopening traffic.", "fact"
    )
    assert result.values.suggested_kind == "procedure"
    assert result.values.taxonomy_value is not None


async def test_durably_useful_memory_gets_non_uncertain_retention(monkeypatch) -> None:
    ok_transport(
        monkeypatch, values_object(retention_disposition="retain", retention_value=0.8)
    )
    result = await assess_content("The production database host is db01.", "fact")
    assert result.values.retention_disposition == "retain"
    assert result.values.retention_value is not None


async def test_genuinely_unclassifiable_content_may_return_taxonomy_null(monkeypatch) -> None:
    ok_transport(
        monkeypatch,
        values_object(
            suggested_kind=None,
            taxonomy_value=None,
            retention_disposition="noise",
            retention_value=0.9,
        ),
    )
    result = await assess_content("packet_version: code_gen.v1,", "fact")
    assert result.values.suggested_kind is None
    assert result.values.taxonomy_value is None
    # But retention stays answerable.
    assert result.values.retention_disposition == "noise"
    assert result.values.retention_value is not None


async def test_genuinely_indeterminate_usefulness_may_return_uncertain(monkeypatch) -> None:
    ok_transport(
        monkeypatch,
        values_object(
            suggested_kind="observation",
            taxonomy_value=0.6,
            retention_disposition="uncertain",
            retention_value=None,
        ),
    )
    result = await assess_content("The batch job emitted a warning at 03:14.", "fact")
    assert result.values.retention_disposition == "uncertain"
    assert result.values.retention_value is None
    # Taxonomy is still answered.
    assert result.values.suggested_kind == "observation"
    assert result.values.taxonomy_value is not None


async def test_truth_uncertainty_does_not_force_abstention(monkeypatch) -> None:
    """Uncertainty about truth/evidence must not push the contract toward null."""
    captured = ok_transport(monkeypatch, values_object())
    await assess_content(
        "Rumor says the LDAP server may have been migrated.", "fact"
    )
    system = system_prompt(captured)
    assert "not about whether the content is factually true" in system
    assert "must NOT push you" in system


async def test_malformed_json_remains_fail_closed(monkeypatch) -> None:
    ok_transport(monkeypatch, "this is not json")
    with pytest.raises(ValidationError):
        await assess_content("Keep deployment audit records.", "fact")


async def test_extra_schema_keys_remain_fail_closed(monkeypatch) -> None:
    message = json.dumps(
        {
            "suggested_kind": "fact",
            "taxonomy_value": 0.8,
            "retention_value": 0.7,
            "retention_disposition": "retain",
            "additionalProperties": False,
        }
    )
    ok_transport(monkeypatch, message)
    # Extra schema keys are a strict-parse failure. SchemaEchoError would be
    # wrong here: the output is a real value object with an extra key, not a
    # skeleton echo, and the strict parser must stay the thing that rejects it.
    with pytest.raises(ValidationError) as excinfo:
        await assess_content("Keep deployment audit records.", "fact")
    assert not isinstance(excinfo.value, SchemaEchoError)


async def test_out_of_vocabulary_output_remains_fail_closed(monkeypatch) -> None:
    message = values_object(retention_disposition="maybe")
    ok_transport(monkeypatch, message)
    with pytest.raises(ValidationError):
        await assess_content("Keep deployment audit records.", "fact")


def test_prompt_identity_bumped_and_old_identity_still_parses() -> None:
    """The corrected contract identity is material and old identities stay distinguishable."""
    assert PROMPT_VERSION == "engram.assess.2"
    assert PROMPT_VERSION != "engram.assess.1"
    current = AssessmentContract(
        provider="openai", model="m", config_version="sha256:" + "0" * 64
    )
    assert current.prompt_version == "engram.assess.2"
    legacy = current.model_copy(update={"prompt_version": "engram.assess.1"})
    assert legacy.prompt_version != current.prompt_version
    # Legacy assessments remain representable as historical evidence.
    assert legacy.model_dump(mode="json")["prompt_version"] == "engram.assess.1"

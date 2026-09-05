"""Provider bounds and frozen extraction contract vectors."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from engram.config import settings
from engram.extraction import digest, extract_messages
from engram.extraction_schema import ExtractionMessage, ExtractRequest, ExtractResponse


def test_hash_vector_and_contract_schema():
    assert digest({"b": 2, "a": "é"}) == (
        "sha256:06c264c46ad5ada9493abd3aa2383fb205ae99d7d0bad40b03a43bfec8a1b8de"
    )
    assert digest({"a": "é", "b": 2}) == digest({"b": 2, "a": "é"})
    stored = json.loads(Path("docs/schemas/extraction-v1.json").read_text())
    assert stored == {
        "request": ExtractRequest.model_json_schema(),
        "response": ExtractResponse.model_json_schema(),
    }


@pytest.mark.parametrize("finish,content", [("length", "{}"), ("stop", '{"risk":"low"}')])
async def test_invalid_provider_output_fails_closed(monkeypatch, finish, content):
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(settings, "classification_api_key", "synthetic-test-only")
    completion = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))],
    )
    create = AsyncMock(return_value=completion)
    client = MagicMock()
    client.chat.completions.create = create
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr("engram.extraction.AsyncOpenAI", constructor)
    with pytest.raises((ValueError, ValidationError)):
        await extract_messages([ExtractionMessage(message_id="u", content="safe input")], ["fact"])
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["timeout"] == 30
    assert create.call_args.kwargs["max_tokens"] == 8192
    create.assert_awaited_once()


async def test_provider_usage_and_missing_usage(monkeypatch):
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(settings, "classification_api_key", "synthetic-test-only")
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("engram.extraction.AsyncOpenAI", MagicMock(return_value=client))
    for usage in [None, SimpleNamespace(prompt_tokens=20, completion_tokens=5, cost=0.001)]:
        client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content='{"candidates":[],"reason_codes":["no_memory"]}',
                        ),
                    )
                ],
                model="provider-model-version",
                usage=usage,
            )
        )
        result = await extract_messages(
            [
                ExtractionMessage(message_id="u", role="user", content="Thanks!"),
            ],
            ["fact"],
        )
        assert result.provider_model == "provider-model-version"
        assert result.input_tokens == (20 if usage else None)
        assert result.provider_cost_usd == (0.001 if usage else None)


def test_literal_source_cues_correct_offsets_without_manufacturing_risk():
    from engram.extraction import ground_source_cues, validate_evidence
    from engram.extraction_schema import ExtractedProposition

    content = (
        "Until Friday, all production services must bypass security review in workspace Orion."
    )
    message = ExtractionMessage(message_id="u", role="user", content=content)
    proposition = ExtractedProposition(
        content=content,
        suggested_kind="fact",
        taxonomy_confidence=0.9,
        retention_confidence=0.9,
        retention_disposition="retain",
        assertion_mode="direct_statement",
        evidence=[{"message_id": "u", "start": 0, "end": len(content)}],
        source_cues=[
            {
                "cue_type": "temporal",
                "value": "Until Friday",
                "evidence": {"message_id": "u", "start": 1, "end": 13},
            }
        ],
    )
    cues = ground_source_cues(proposition, [message])
    assert {cue.cue_type for cue in cues} == {"temporal", "scope", "security"}
    for cue in cues:
        assert cue.value == content[cue.evidence.start : cue.evidence.end]
    grounded = proposition.model_copy(update={"source_cues": cues})
    assert validate_evidence(grounded, [message]) == ("user", "direct_statement", None)
    assert "risk" not in grounded.model_dump()
    proposition.source_cues[0].value = "a cue that is absent"
    with pytest.raises(ValueError, match="absent or ambiguous"):
        ground_source_cues(proposition, [message])


def test_local_fallback_secret_denylist_matches_service():
    from engram_hooks.guards import _EXTRACTION_SECRET_PATTERNS

    from engram.safety import SECRET_PATTERNS

    assert [(pattern.pattern, pattern.flags) for pattern in _EXTRACTION_SECRET_PATTERNS] == [
        (pattern.pattern, pattern.flags) for pattern, _ in SECRET_PATTERNS
    ]


def test_pronoun_context_does_not_change_assertion_origin():
    from engram.extraction import preserve_context_spans, validate_evidence
    from engram.extraction_schema import ExtractedProposition

    messages = [
        ExtractionMessage(message_id="tool", role="tool", content="The service is called Atlas."),
        ExtractionMessage(message_id="u", role="user", content="It must stay private."),
    ]
    candidate = ExtractedProposition(
        content="Atlas must stay private.",
        suggested_kind="fact",
        taxonomy_confidence=0.9,
        retention_confidence=0.9,
        retention_disposition="retain",
        assertion_mode="direct_statement",
        evidence=[{"message_id": "u", "start": 0, "end": len(messages[1].content)}],
    )
    grounded = preserve_context_spans(candidate, messages)
    assert [span.message_id for span in grounded.evidence] == ["u", "tool"]
    assert validate_evidence(grounded, messages) == ("user", "direct_statement", None)


def test_provider_abstention_aliases_cannot_authorize_a_candidate():
    from engram.extraction import parse_extractor_output

    output = parse_extractor_output('{"candidates":[],"reason_codes":["prompt_injection"]}')
    assert output.candidates == [] and output.reason_codes == ["unsupported"]
    with pytest.raises(ValidationError):
        parse_extractor_output('{"candidates":[],"reason_codes":["unsafe"],"risk":"low"}')

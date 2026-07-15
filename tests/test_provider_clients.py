"""Shared classification-provider resolution and call-site contract."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import engram.classification as classification
import engram.conflicts as conflicts
from engram.config import settings
from engram.provider_clients import resolve_classification_provider


class _Completions:
    def __init__(self, content: str) -> None:
        self._content = content

    async def create(self, **_kwargs: object) -> object:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))],
            usage=None,
        )


class _Client:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(completions=_Completions(content))


@pytest.fixture
def classification_provider(monkeypatch):
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(settings, "classification_api_key", "classification-only-key")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(
        settings,
        "classification_base_url",
        "https://user:password@compatible.example.test/v1/chat?credential=hidden",
    )
    monkeypatch.setattr(settings, "openai_base_url", None)
    monkeypatch.setattr(settings, "classification_model", "compatible-model")


async def test_general_and_conflict_share_resolved_provider(monkeypatch, classification_provider):
    constructor_calls: list[dict[str, object]] = []
    events: list[dict[str, object]] = []

    def general_client(**kwargs: object) -> _Client:
        constructor_calls.append(kwargs)
        return _Client('{"suggested_kind":"fact","confidence":0.8,"reason":"ok"}')

    def conflict_client(**kwargs: object) -> _Client:
        constructor_calls.append(kwargs)
        return _Client('{"verdict":"refine","confidence":0.7,"reason":"detail"}')

    async def capture(**kwargs: object) -> None:
        events.append(kwargs)

    monkeypatch.setattr(classification, "AsyncOpenAI", general_client)
    monkeypatch.setattr(conflicts, "AsyncOpenAI", conflict_client)
    monkeypatch.setattr(classification, "record_provider_call", capture)
    monkeypatch.setattr(conflicts, "record_provider_call", capture)

    tenant_id = uuid4()
    await classification._call_openai_classification("safe prompt", tenant_id=tenant_id)
    await conflicts._classify_relationship_llm("old", "new", 0.9, tenant_id=tenant_id)

    assert constructor_calls == [
        {
            "api_key": "classification-only-key",
            "base_url": (
                "https://user:password@compatible.example.test/v1/chat?credential=hidden"
            ),
        },
    ] * 2
    assert {event["provider_host"] for event in events} == {"compatible.example.test"}
    assert {event["model"] for event in events} == {"compatible-model"}
    assert all(event["provider_adapter"] == "openai" for event in events)
    persisted = repr(events)
    assert "classification-only-key" not in persisted
    assert "password" not in persisted
    assert "/v1/chat" not in persisted
    assert "credential=hidden" not in persisted


def test_resolution_contract_uses_classification_fallbacks(classification_provider):
    resolved = resolve_classification_provider()
    assert resolved.api_key == "classification-only-key"
    assert resolved.base_url == settings.classification_base_url
    assert resolved.model == "compatible-model"
    assert resolved.sanitized_provider_host == "compatible.example.test"


async def test_classification_client_setup_failure_is_non_attempted(
    monkeypatch, classification_provider
):
    events: list[dict[str, object]] = []

    async def capture(**kwargs: object) -> None:
        events.append(kwargs)

    def broken_client(**_kwargs: object) -> object:
        raise ValueError("credential must not be persisted")

    monkeypatch.setattr(classification, "AsyncOpenAI", broken_client)
    monkeypatch.setattr(classification, "record_provider_call", capture)
    with pytest.raises(ValueError):
        await classification._call_openai_classification("private prompt", tenant_id=uuid4())
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["external_call_attempted"] is False
    assert events[0]["metadata"] == {
        "application_fallback": True,
        "failure_stage": "client_setup",
        "error_type": "ValueError",
    }
    assert "credential" not in repr(events)
    assert "private prompt" not in repr(events)


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict":"invented","confidence":0.99,"reason":"bad"}',
        '{"confidence":0.99,"reason":"missing"}',
        '{"verdict":"duplicate","confidence":"high","reason":"bad"}',
        '{"verdict":"duplicate","confidence":NaN,"reason":"bad"}',
        '{"verdict":"duplicate","confidence":Infinity,"reason":"bad"}',
        '{"verdict":"duplicate","confidence":1.1,"reason":"bad"}',
    ],
)
async def test_invalid_conflict_payload_uses_heuristic_fallback(
    monkeypatch, classification_provider, payload
):
    events: list[dict[str, object]] = []

    async def capture(**kwargs: object) -> None:
        events.append(kwargs)

    monkeypatch.setattr(conflicts, "AsyncOpenAI", lambda **_kwargs: _Client(payload))
    monkeypatch.setattr(conflicts, "record_provider_call", capture)

    verdict, confidence, _reason, provenance = await conflicts._classify_relationship(
        "old", "new", 0.9, tenant_id=uuid4(), usage_class="async_enrichment"
    )

    assert verdict is conflicts.ConflictVerdict.REFINE
    assert confidence == 0.5
    assert provenance["mode"] == "fallback"
    assert provenance["invalid_provider_response"] is True
    assert len(events) == 1
    event = events[0]
    assert event["status"] == "failed"
    assert event["external_call_attempted"] is True
    assert event["usage_class"] == "async_enrichment"
    metadata = event["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["failure_stage"] == "response_validation"
    assert metadata["application_fallback"] is True


@pytest.mark.parametrize(
    ("response", "stage"),
    [
        (SimpleNamespace(choices=[], usage=None), "response_validation"),
        (SimpleNamespace(choices=[SimpleNamespace()], usage=None), "response_validation"),
        (
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=123))], usage=None
            ),
            "response_validation",
        ),
        (
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
                usage=None,
            ),
            "response_parse",
        ),
        (
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))], usage=None
            ),
            "response_parse",
        ),
    ],
)
async def test_general_response_envelope_records_exactly_one_failure(
    monkeypatch, classification_provider, response, stage
):
    events: list[dict[str, object]] = []

    async def capture(**kwargs: object) -> None:
        events.append(kwargs)

    client = _Client("unused")
    client.chat.completions = _APIResponse(response)
    monkeypatch.setattr(classification, "AsyncOpenAI", lambda **_kwargs: client)
    monkeypatch.setattr(classification, "record_provider_call", capture)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        await classification._call_openai_classification("prompt", tenant_id=uuid4())
    assert len(events) == 1
    metadata = events[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["failure_stage"] == stage


class _APIResponse:
    def __init__(self, response: object) -> None:
        self.response = response

    async def create(self, **_kwargs: object) -> object:
        return self.response

"""Untrusted embedding-response validation and single-event accounting."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import engram.embeddings as embeddings
from engram.config import settings


class _API:
    def __init__(self, response: object) -> None:
        self.response = response

    async def create(self, **_kwargs: object) -> object:
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.embeddings = _API(response)


def _response(items: list[object]) -> object:
    return SimpleNamespace(
        data=items,
        usage=SimpleNamespace(prompt_tokens=7, total_tokens=7, cost=0.004),
    )


def _item(index: object, vector: object) -> object:
    return SimpleNamespace(index=index, embedding=vector)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    monkeypatch.setattr(settings, "openai_api_key", "test-only")


async def _run(monkeypatch, response: object) -> tuple[list[list[float] | None], list[dict]]:
    events: list[dict] = []

    async def capture(**kwargs: object) -> None:
        events.append(dict(kwargs))

    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: _Client(response))
    monkeypatch.setattr(embeddings, "record_provider_call", capture)
    vectors = await embeddings.generate_embeddings(
        ["first", "second"], tenant_id=uuid4(), usage_class="request"
    )
    return vectors, events


async def test_embedding_response_reordered_by_provider_index(monkeypatch, provider):
    vectors, events = await _run(
        monkeypatch,
        _response([_item(1, [3, 4]), _item(0, [1, 2])]),
    )
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]
    assert len(events) == 1
    assert events[0]["status"] == "succeeded"


@pytest.mark.parametrize(
    "items",
    [
        [_item(0, [1, 2]), _item(0, [3, 4])],
        [_item(0, [1, 2])],
        [_item(0, [1, 2]), _item(2, [3, 4])],
        [_item(-1, [1, 2]), _item(1, [3, 4])],
        [_item(0, [1, float("nan")]), _item(1, [3, 4])],
        [_item(0, [1, float("inf")]), _item(1, [3, 4])],
        [_item(0, [1]), _item(1, [3, 4])],
    ],
)
async def test_invalid_embedding_response_records_one_failure(
    monkeypatch, provider, items
):
    events: list[dict] = []

    async def capture(**kwargs: object) -> None:
        events.append(dict(kwargs))

    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: _Client(_response(items)))
    monkeypatch.setattr(embeddings, "record_provider_call", capture)
    with pytest.raises((TypeError, ValueError)):
        await embeddings.generate_embeddings(
            ["first", "second"], tenant_id=uuid4(), usage_class="request"
        )
    assert len(events) == 1
    event = events[0]
    assert event["status"] == "failed"
    assert event["external_call_attempted"] is True
    assert event["total_tokens"] == 7
    assert event["reported_cost_usd"] == pytest.approx(0.004)
    assert event["metadata"]["failure_stage"] == "response_validation"


async def test_embedding_client_setup_failure_is_not_external(monkeypatch, provider):
    events: list[dict] = []

    async def capture(**kwargs: object) -> None:
        events.append(dict(kwargs))

    def broken_client(**_kwargs: object) -> object:
        raise ValueError("configuration included secret material")

    monkeypatch.setattr("openai.AsyncOpenAI", broken_client)
    monkeypatch.setattr(embeddings, "record_provider_call", capture)
    with pytest.raises(ValueError):
        await embeddings.generate_embedding(
            "private content", tenant_id=uuid4(), usage_class="async_enrichment"
        )
    assert len(events) == 1
    assert events[0]["external_call_attempted"] is False
    assert events[0]["metadata"] == {
        "failure_stage": "client_setup",
        "error_type": "ValueError",
    }
    assert "secret" not in repr(events)
    assert "private content" not in repr(events)

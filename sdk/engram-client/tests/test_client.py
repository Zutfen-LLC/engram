"""Tests for the Engram SDK client.

Uses :class:`httpx.MockTransport` to exercise every method without a live
server, and asserts on request shape (method, path, auth header, body/params)
plus response parsing and typed error handling.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest

from engram_client import (
    ClassifyResponse,
    DiaryWriteResponse,
    EngramAuthError,
    EngramClient,
    EngramClientError,
    EngramError,
    EngramHTTPError,
    EngramNotFoundError,
    EngramServerError,
    EngramValidationError,
    KgAddResponse,
    KgTripleOut,
    RecallResponse,
    RememberResponse,
    SearchResponse,
)

ITEM_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"

_REMEMBER_CREATED: dict[str, Any] = {
    "id": ITEM_ID,
    "status": "created",
    "review_status": "active",
    "memory_confidence": 0.9,
}


class _Recorder:
    """A MockTransport handler that captures the request and returns canned JSON."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        checker: Callable[[httpx.Request], None] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.checker = checker
        self.request: httpx.Request | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if self.checker is not None:
            self.checker(request)
        return httpx.Response(self.status_code, json=self.payload)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = "k",
) -> EngramClient:
    return EngramClient(
        "http://engram.test/",
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ---- remember ----


async def test_remember_success() -> None:
    rec = _Recorder(
        status_code=201,
        payload={
            "id": ITEM_ID,
            "status": "created",
            "review_status": "active",
            "memory_confidence": 0.9,
        },
    )
    client = _client(rec)
    try:
        resp = await client.remember(
            "always use lowercase table names", kind="invariant", importance=0.8
        )
    finally:
        await client.close()

    assert isinstance(resp, RememberResponse)
    assert resp.id == UUID(ITEM_ID)
    assert resp.status == "created"
    assert resp.review_status == "active"
    assert resp.memory_confidence == pytest.approx(0.9)

    assert rec.request is not None
    assert rec.request.method == "POST"
    assert rec.request.url.path == "/v1/remember"
    assert rec.request.headers["authorization"] == "Bearer k"
    body = _body(rec.request)
    assert body["content"] == "always use lowercase table names"
    assert body["kind"] == "invariant"
    assert body["importance"] == 0.8
    assert body["source_type"] == "manual"
    # exclude_none drops unset optional fields
    assert "wing" not in body
    assert "external_id" not in body


async def test_remember_deduped_response() -> None:
    rec = _Recorder(
        status_code=201,
        payload={
            "id": ITEM_ID,
            "status": "deduped",
            "review_status": "active",
            "memory_confidence": 0.9,
            "deduped_existing_id": OTHER_ID,
        },
    )
    async with _client(rec) as client:
        resp = await client.remember("dup content")
    assert resp.status == "deduped"
    assert resp.deduped_existing_id == UUID(OTHER_ID)


async def test_remember_no_api_key_omits_auth_header() -> None:
    rec = _Recorder(status_code=201, payload=_REMEMBER_CREATED)

    def check(request: httpx.Request) -> None:
        assert "authorization" not in request.headers

    rec.checker = check
    async with _client(rec, api_key=None) as client:
        await client.remember("x")


# ---- recall ----


async def test_recall_success() -> None:
    rec = _Recorder(
        payload={
            "working_set": "- [invariant] hello",
            "item_count": 1,
            "byte_count": 32,
            "omitted_count": 0,
            "items": [{"id": ITEM_ID, "kind": "invariant"}],
            "recall_log_id": OTHER_ID,
        },
    )
    async with _client(rec) as client:
        resp = await client.recall(byte_budget=2048)
    assert isinstance(resp, RecallResponse)
    assert resp.item_count == 1
    assert resp.items[0]["kind"] == "invariant"
    assert rec.request is not None
    body = _body(rec.request)
    assert body["mode"] == "startup"
    assert body["byte_budget"] == 2048
    assert "query" not in body


async def test_recall_semantic_mode_sends_query() -> None:
    """BL-004: recall(mode='semantic', query='...') reaches the API with both
    fields in the body and parses a RecallResponse (including the new optional
    ``message`` field)."""
    rec = _Recorder(
        payload={
            "working_set": "- [fact] semantic target",
            "item_count": 1,
            "byte_count": 40,
            "omitted_count": 0,
            "items": [
                {
                    "id": ITEM_ID,
                    "kind": "fact",
                    "content": "semantic target",
                    "score": 0.92,
                    "reasons": ["semantic similarity 0.92"],
                    "warnings": [],
                }
            ],
            "recall_log_id": OTHER_ID,
            "scoring_version": "semantic-v1",
            "message": None,
        },
    )
    async with _client(rec) as client:
        resp = await client.recall(mode="semantic", query="semantic query", item_budget=5)
    assert isinstance(resp, RecallResponse)
    assert resp.scoring_version == "semantic-v1"
    assert resp.message is None
    assert resp.items[0]["reasons"] == ["semantic similarity 0.92"]
    assert rec.request is not None
    body = _body(rec.request)
    assert body["mode"] == "semantic"
    assert body["query"] == "semantic query"
    assert body["item_budget"] == 5


async def test_recall_semantic_message_field_parsed() -> None:
    """The new ``message`` field survives response_model validation."""
    rec = _Recorder(
        payload={
            "working_set": "",
            "item_count": 0,
            "byte_count": 0,
            "omitted_count": 0,
            "items": [],
            "recall_log_id": OTHER_ID,
            "message": "No embeddings are available yet.",
        },
    )
    async with _client(rec) as client:
        resp = await client.recall(mode="semantic", query="x")
    assert isinstance(resp, RecallResponse)
    assert resp.item_count == 0
    assert resp.message == "No embeddings are available yet."


# ---- search ----


async def test_search_success() -> None:
    rec = _Recorder(
        payload={
            "results": [{"id": ITEM_ID, "content": "hit", "score": 0.9, "mode": "hybrid"}],
            "total": 1,
        },
    )
    async with _client(rec) as client:
        resp = await client.search("table names", mode="keyword", limit=5)
    assert isinstance(resp, SearchResponse)
    assert resp.total == 1
    assert resp.results[0]["content"] == "hit"
    assert rec.request is not None
    body = _body(rec.request)
    assert body["query"] == "table names"
    assert body["mode"] == "keyword"
    assert body["limit"] == 5


# ---- classify ----


async def test_classify_success() -> None:
    rec = _Recorder(
        payload={
            "suggested_kind": "invariant",
            "suggested_wing": "engineering",
            "suggested_room": "conventions",
            "confidence": 0.82,
            "reason": "rule: 'always'",
            "rules_matched": ["always_keyword"],
        },
    )
    async with _client(rec) as client:
        resp = await client.classify("always use lowercase", context="chat excerpt")
    assert isinstance(resp, ClassifyResponse)
    assert resp.suggested_kind == "invariant"
    assert resp.confidence == pytest.approx(0.82)
    body = _body(rec.request)
    assert body["content"] == "always use lowercase"
    assert body["context"] == "chat excerpt"


# ---- kg_add ----


async def test_kg_add_success() -> None:
    rec = _Recorder(
        status_code=201,
        payload={
            "id": OTHER_ID,
            "triple": {"subject": "users", "predicate": "located_in", "object": "us-east-1"},
            "source_item_id": ITEM_ID,
        },
    )
    async with _client(rec) as client:
        resp = await client.kg_add(
            "users", "located_in", "us-east-1", source_item_id=ITEM_ID, confidence=0.7
        )
    assert isinstance(resp, KgAddResponse)
    assert resp.id == UUID(OTHER_ID)
    assert resp.triple["predicate"] == "located_in"
    assert resp.source_item_id == UUID(ITEM_ID)
    body = _body(rec.request)
    assert body["subject"] == "users"
    assert body["object"] == "us-east-1"
    assert body["source_item_id"] == ITEM_ID
    assert body["confidence"] == 0.7
    assert "workspace" not in body


# ---- kg_query ----


async def test_kg_query_success() -> None:
    rec = _Recorder(
        payload=[
            {
                "id": OTHER_ID,
                "subject": "users",
                "predicate": "located_in",
                "object": "us-east-1",
                "confidence": 0.7,
                "review_status": "active",
                "created_at": "2026-07-07T12:00:00Z",
                "trust_annotation": None,
            }
        ],
    )
    async with _client(rec) as client:
        results = await client.kg_query("users", direction="outbound", predicate="located_in")
    assert len(results) == 1
    assert isinstance(results[0], KgTripleOut)
    assert results[0].object == "us-east-1"
    assert rec.request is not None
    assert rec.request.method == "GET"
    assert rec.request.url.path == "/v1/kg/query"
    assert rec.request.url.params["entity"] == "users"
    assert rec.request.url.params["direction"] == "outbound"
    assert rec.request.url.params["predicate"] == "located_in"
    assert "as_of" not in rec.request.url.params


# ---- diary_write ----


async def test_diary_write_success() -> None:
    rec = _Recorder(
        status_code=201,
        payload={
            "id": ITEM_ID,
            "status": "created",
            "review_status": "proposed",
            "principal_id": OTHER_ID,
        },
    )
    async with _client(rec) as client:
        resp = await client.diary_write("explored the search path", "hermes", topic="debugging")
    assert isinstance(resp, DiaryWriteResponse)
    assert resp.status == "created"
    assert resp.principal_id == UUID(OTHER_ID)
    body = _body(rec.request)
    assert body["entry"] == "explored the search path"
    assert body["principal"] == "hermes"
    assert body["topic"] == "debugging"


# ---- error handling ----


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (400, EngramClientError),
        (401, EngramAuthError),
        (403, EngramAuthError),
        (404, EngramNotFoundError),
        (409, EngramClientError),
        (422, EngramValidationError),
        (500, EngramServerError),
        (503, EngramServerError),
    ],
)
async def test_typed_errors(status: int, exc_type: type[EngramHTTPError]) -> None:
    rec = _Recorder(status_code=status, payload={"detail": f"boom {status}"})
    async with _client(rec) as client:
        with pytest.raises(exc_type) as exc_info:
            await client.remember("x")
    assert isinstance(exc_info.value, EngramHTTPError)
    assert isinstance(exc_info.value, EngramError)
    assert exc_info.value.status_code == status
    assert exc_info.value.detail == f"boom {status}"


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (404, EngramNotFoundError),
        (409, EngramClientError),
        (422, EngramValidationError),
    ],
)
async def test_typed_errors_with_structured_detail(
    status: int, exc_type: type[EngramHTTPError]
) -> None:
    """BL-003: the API's DB-constraint-mapped error body is a ``detail``
    object (``{message, code, constraint}``), not a plain string — the SDK
    must classify purely on status code and pass the object through as-is."""
    structured_detail = {
        "message": "request rejected by database constraint (check_violation): chk_kind",
        "code": "check_violation",
        "constraint": "chk_kind",
    }
    rec = _Recorder(status_code=status, payload={"detail": structured_detail})
    async with _client(rec) as client:
        with pytest.raises(exc_type) as exc_info:
            await client.remember("x")
    assert exc_info.value.status_code == status
    assert exc_info.value.detail == structured_detail


async def test_error_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502, content="<html>bad gateway</html>", headers={"content-type": "text/html"}
        )

    async with _client(handler) as client:
        with pytest.raises(EngramServerError) as exc_info:
            await client.recall()
    assert exc_info.value.status_code == 502
    # detail falls back to the raw body text
    assert "bad gateway" in str(exc_info.value)


async def test_validation_error_detail_can_be_list() -> None:
    detail: list[dict[str, Any]] = [{"loc": ["body", "limit"], "msg": "too big"}]
    rec = _Recorder(status_code=422, payload={"detail": detail})

    def check(request: httpx.Request) -> None:
        assert request.url.path == "/v1/classify"

    rec.checker = check
    async with _client(rec) as client:
        with pytest.raises(EngramValidationError) as exc_info:
            await client.classify("x")
    assert exc_info.value.status_code == 422
    assert isinstance(exc_info.value.detail, list)


async def test_search_limit_validated_client_side() -> None:
    """SearchRequest enforces limit bounds before hitting the network."""
    from pydantic import ValidationError

    async with _client(lambda req: httpx.Response(200, json={})) as client:
        with pytest.raises(ValidationError):
            await client.search("x", limit=999)


# ---- context manager ----


async def test_context_manager_closes_client() -> None:
    rec = _Recorder(status_code=201, payload=_REMEMBER_CREATED)
    client = _client(rec)
    assert not client.httpx_client.is_closed
    async with client:
        await client.remember("inside ctx")
    assert client.httpx_client.is_closed


async def test_base_url_trailing_slash_normalized() -> None:
    rec = _Recorder(status_code=201, payload=_REMEMBER_CREATED)
    client = EngramClient(
        "http://engram.test///",
        api_key="k",
        transport=httpx.MockTransport(rec),
    )
    async with client:
        await client.remember("x")
    assert rec.request is not None
    assert rec.request.url.path == "/v1/remember"


async def test_plaintext_http_warns_for_non_loopback_only() -> None:
    """An api_key over non-loopback http warns; loopback/https stay silent."""
    rec = _Recorder(status_code=201, payload=_REMEMBER_CREATED)

    # non-loopback http -> warns (pytest.warns overrides the suite ignore filter)
    with pytest.warns(UserWarning, match="plaintext http"):
        client = EngramClient(
            "http://engram.test/", api_key="k", transport=httpx.MockTransport(rec)
        )
    await client.close()

    # loopback http and https -> no warning (any warning becomes an error)
    for base in ("http://localhost:8000", "http://127.0.0.1:8000", "https://engram.test/"):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            client = EngramClient(base, api_key="k", transport=httpx.MockTransport(rec))
        await client.close()

    # no api_key -> no warning even over non-loopback http
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client = EngramClient(
            "http://engram.test/", api_key=None, transport=httpx.MockTransport(rec)
        )
    await client.close()

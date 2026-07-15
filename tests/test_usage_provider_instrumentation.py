"""Provider-call instrumentation tests for ENG-METER-001.

Requires a live PostgreSQL with the v2 schema. Skips automatically when no DB
is reachable. Exercises the REAL provider-call functions (not test doubles
that replace the whole function) with a fake OpenAI client, so the real
usage-extraction and telemetry-recording code paths actually run.

Covers: classification success/failure+fallback, conflict-classification
success/fallback, single + batched embedding (input_count), semantic-recall
and semantic/hybrid-search query embeddings, embedding_setup exclusion from
normal operations, telemetry surviving a business-transaction rollback, and a
telemetry insertion failure not failing the business operation.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import engram.classification as classification_mod
import engram.conflicts as conflicts_mod
import engram.embeddings as embeddings_mod
from engram.config import settings

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_test_session_factory = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(autouse=True)
async def _fresh_engine():
    global _test_engine, _test_session_factory
    _test_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    _test_session_factory = async_sessionmaker(
        _test_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await _test_engine.dispose()


async def _db_ok() -> bool:
    try:
        async with _test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
async def _clean_usage_events():
    if not await _db_ok():
        return
    async with _test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM usage_events"))


@pytest.fixture(autouse=True)
def _enable_telemetry(monkeypatch):
    monkeypatch.setattr(settings, "usage_telemetry_enabled", True)


async def _default_tenant_id() -> str:
    from engram.db import _DEFAULT_TENANT_SLUG

    async with _test_session_factory() as session:
        return (
            await session.execute(
                text("SELECT id::text FROM tenants WHERE slug = :slug"),
                {"slug": _DEFAULT_TENANT_SLUG},
            )
        ).scalar_one()


async def _provider_calls(operation: str) -> list[dict]:
    async with _test_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM usage_events WHERE event_type = 'provider.call' "
                    "AND operation = :op"
                ),
                {"op": operation},
            )
        ).mappings().all()
        return [dict(r) for r in rows]


# ---- Fake OpenAI client plumbing ----


@dataclass
class _FakeUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeChatResponse:
    def __init__(self, content: str, usage: _FakeUsage | None) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = usage


class _FakeChatCompletions:
    def __init__(self, response: _FakeChatResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc

    async def create(self, **kwargs: object) -> _FakeChatResponse:
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


class _FakeChat:
    def __init__(self, completions: _FakeChatCompletions) -> None:
        self.completions = completions


class _FakeEmbeddingItem:
    def __init__(self, embedding: list[float], index: int) -> None:
        self.embedding = embedding
        self.index = index


class _FakeEmbeddingsResponse:
    def __init__(self, vectors: list[list[float]], usage: _FakeUsage | None) -> None:
        self.data = [_FakeEmbeddingItem(v, index) for index, v in enumerate(vectors)]
        self.usage = usage


class _FakeEmbeddingsAPI:
    def __init__(
        self, response: _FakeEmbeddingsResponse | None = None, exc: Exception | None = None
    ):
        self._response = response
        self._exc = exc

    async def create(self, **kwargs: object) -> _FakeEmbeddingsResponse:
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


class FakeAsyncOpenAI:
    def __init__(
        self,
        *,
        chat_response: _FakeChatResponse | None = None,
        chat_exc: Exception | None = None,
        embeddings_response: _FakeEmbeddingsResponse | None = None,
        embeddings_exc: Exception | None = None,
        **_kwargs: object,
    ) -> None:
        self.chat = _FakeChat(_FakeChatCompletions(chat_response, chat_exc))
        self.embeddings = _FakeEmbeddingsAPI(embeddings_response, embeddings_exc)


# ---- classification ----


async def test_classification_success_records_tokens(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "openai")
    response = _FakeChatResponse(
        content='{"suggested_kind": "fact", "confidence": 0.8, "reason": "ok"}',
        usage=_FakeUsage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
    )
    monkeypatch.setattr(
        classification_mod, "AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(chat_response=response)
    )

    await classification_mod._call_openai_classification(
        "classify this", tenant_id=tenant_id
    )

    calls = await _provider_calls("classification")
    assert len(calls) == 1
    assert calls[0]["status"] == "succeeded"
    assert calls[0]["external_call_attempted"] is True
    assert calls[0]["usage_class"] == "request"
    assert calls[0]["prompt_tokens"] == 50
    assert calls[0]["completion_tokens"] == 10
    assert calls[0]["total_tokens"] == 60
    assert calls[0]["input_count"] == 1


async def test_classification_failure_records_failed_with_fallback_metadata(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(
        classification_mod,
        "AsyncOpenAI",
        lambda **kw: FakeAsyncOpenAI(chat_exc=RuntimeError("provider down")),
    )

    with pytest.raises(RuntimeError):
        await classification_mod._call_openai_classification("classify this", tenant_id=tenant_id)

    calls = await _provider_calls("classification")
    assert len(calls) == 1
    # status is the provider outcome only (failed), not a 'fallback' status;
    # the application fallback is recorded as metadata.
    assert calls[0]["status"] == "failed"
    assert calls[0]["external_call_attempted"] is True
    assert calls[0]["prompt_tokens"] is None
    metadata = calls[0]["metadata"]
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["application_fallback"] is True
    assert metadata["failure_stage"] == "provider_error"
    assert metadata["error_type"] == "RuntimeError"
    assert "provider down" not in str(metadata)


async def test_classification_disabled_records_disabled_status(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "none")

    async with _test_session_factory() as session:
        await classification_mod.classify("some content", tenant_id, session)

    calls = await _provider_calls("classification")
    assert len(calls) == 1
    assert calls[0]["status"] == "disabled"
    assert calls[0]["external_call_attempted"] is False


# ---- conflict_classification ----


async def test_conflict_classification_success(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "openai")
    response = _FakeChatResponse(
        content='{"verdict": "duplicate", "confidence": 0.9, "reason": "same"}',
        usage=_FakeUsage(prompt_tokens=30, completion_tokens=5, total_tokens=35),
    )
    monkeypatch.setattr(
        conflicts_mod, "AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(chat_response=response)
    )

    await conflicts_mod._classify_relationship_llm(
        "old content", "new content", 0.9, tenant_id=tenant_id
    )

    calls = await _provider_calls("conflict_classification")
    assert len(calls) == 1
    assert calls[0]["status"] == "succeeded"
    assert calls[0]["total_tokens"] == 35
    metadata = calls[0]["metadata"]
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["verdict"] == "duplicate"


async def test_conflict_classification_failure_records_failed(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(
        conflicts_mod,
        "AsyncOpenAI",
        lambda **kw: FakeAsyncOpenAI(chat_exc=RuntimeError("provider down")),
    )

    with pytest.raises(RuntimeError):
        await conflicts_mod._classify_relationship_llm(
            "old content", "new content", 0.9, tenant_id=tenant_id
        )

    calls = await _provider_calls("conflict_classification")
    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    metadata = calls[0]["metadata"]
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["application_fallback"] is True
    assert metadata["failure_stage"] == "provider_error"
    assert metadata["error_type"] == "RuntimeError"
    assert "provider down" not in str(metadata)


async def test_classification_malformed_json_records_response_parse(monkeypatch):
    """A provider response that is not valid JSON (a common OpenAI-compatible
    failure mode) must be recorded as a failed provider call with
    ``failure_stage=response_parse`` — previously ``json.loads`` raised before
    the isinstance branch could record it, so the failure vanished from the
    ledger (ENG-METER-001 correction)."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "openai")
    # The response carries usage (the provider DID deliver a billed response)
    # but the content is not valid JSON.
    response = _FakeChatResponse(
        content="<<not json at all",
        usage=_FakeUsage(prompt_tokens=40, completion_tokens=8, total_tokens=48, cost=0.006),
    )
    monkeypatch.setattr(
        classification_mod, "AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(chat_response=response)
    )

    with pytest.raises(ValueError):
        await classification_mod._call_openai_classification("classify this", tenant_id=tenant_id)

    calls = await _provider_calls("classification")
    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    # Usage from the delivered-but-unusable response is preserved.
    assert calls[0]["total_tokens"] == 48
    assert float(calls[0]["reported_cost_usd"]) == pytest.approx(0.006)
    metadata = calls[0]["metadata"]
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["failure_stage"] == "response_parse"
    assert metadata["error_type"] == "JSONDecodeError"
    assert metadata["application_fallback"] is True


async def test_conflict_classification_malformed_json_records_response_parse(monkeypatch):
    """Same malformed-JSON coverage for the conflict-classification LLM path."""
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "classification_provider", "openai")
    response = _FakeChatResponse(
        content="{broken json",
        usage=_FakeUsage(prompt_tokens=20, total_tokens=20, cost=0.003),
    )
    monkeypatch.setattr(
        conflicts_mod, "AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(chat_response=response)
    )

    with pytest.raises(ValueError):
        await conflicts_mod._classify_relationship_llm(
            "old content", "new content", 0.9, tenant_id=tenant_id
        )

    calls = await _provider_calls("conflict_classification")
    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    assert calls[0]["total_tokens"] == 20
    assert float(calls[0]["reported_cost_usd"]) == pytest.approx(0.003)
    metadata = calls[0]["metadata"]
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["failure_stage"] == "response_parse"
    assert metadata["error_type"] == "JSONDecodeError"


async def test_embedding_transport_failure_has_safe_metadata(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        lambda **kw: FakeAsyncOpenAI(embeddings_exc=RuntimeError("secret query provider body")),
    )

    with pytest.raises(RuntimeError):
        await embeddings_mod.generate_embedding("private query", tenant_id=tenant_id)

    calls = await _provider_calls("embedding_document")
    assert len(calls) == 1
    metadata = calls[0]["metadata"]
    assert metadata["failure_stage"] == "provider_error"
    assert metadata["error_type"] == "RuntimeError"
    assert "secret" not in str(metadata)
    assert "private query" not in str(metadata)


@pytest.mark.parametrize(
    ("vectors", "expected_error"),
    [
        ([[0.1, 0.2, 0.3]], "vector count"),
        ([[0.1, 0.2], [0.3, 0.4]], "vector dimension"),
    ],
)
async def test_embedding_response_validation_records_one_failed_call_with_usage(
    monkeypatch, vectors, expected_error
):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    response = _FakeEmbeddingsResponse(
        vectors=vectors,
        usage=_FakeUsage(prompt_tokens=7, total_tokens=7, cost=0.004),
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=response)
    )

    with pytest.raises(ValueError, match=expected_error):
        await embeddings_mod.generate_embeddings(["first", "second"], tenant_id=tenant_id)

    calls = await _provider_calls("embedding_document")
    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    assert calls[0]["total_tokens"] == 7
    assert float(calls[0]["reported_cost_usd"]) == pytest.approx(0.004)
    metadata = calls[0]["metadata"]
    assert metadata == {
        "failure_stage": "response_validation",
        "error_type": "ValueError",
        "expected_vector_count": 2,
        "returned_vector_count": len(vectors),
        "expected_dimensions": 3,
        "offending_index_present": len(vectors) == 2,
    }


# ---- embeddings ----


async def test_single_embedding_document_input_count_one(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]], usage=_FakeUsage(prompt_tokens=5, total_tokens=5)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    await embeddings_mod.generate_embedding(
        "one document", tenant_id=tenant_id, operation="embedding_document"
    )

    calls = await _provider_calls("embedding_document")
    assert len(calls) == 1
    assert calls[0]["input_count"] == 1
    assert calls[0]["total_tokens"] == 5


async def test_batch_embedding_input_count_greater_than_one(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]] * 4, usage=_FakeUsage(prompt_tokens=20, total_tokens=20)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    await embeddings_mod.generate_embeddings(
        ["a", "b", "c", "d"], tenant_id=tenant_id, operation="embedding_backfill"
    )

    calls = await _provider_calls("embedding_backfill")
    assert len(calls) == 1
    assert calls[0]["input_count"] == 4


async def test_semantic_recall_query_embedding_operation_tag(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]], usage=_FakeUsage(prompt_tokens=3, total_tokens=3)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    await embeddings_mod.generate_embedding(
        "recall query", tenant_id=tenant_id, operation="embedding_query_recall"
    )
    calls = await _provider_calls("embedding_query_recall")
    assert len(calls) == 1


async def test_semantic_search_query_embedding_operation_tag(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]], usage=_FakeUsage(prompt_tokens=3, total_tokens=3)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    await embeddings_mod.generate_embedding(
        "search query", tenant_id=tenant_id, operation="embedding_query_search"
    )
    calls = await _provider_calls("embedding_query_search")
    assert len(calls) == 1


async def test_embedding_setup_is_a_distinct_operation_excluded_from_others(monkeypatch):
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]], usage=_FakeUsage(prompt_tokens=1, total_tokens=1)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    await embeddings_mod.generate_embedding(
        "setup ping", tenant_id=tenant_id, operation="embedding_setup"
    )

    setup_calls = await _provider_calls("embedding_setup")
    document_calls = await _provider_calls("embedding_document")
    assert len(setup_calls) == 1
    assert len(document_calls) == 0


async def test_provider_call_survives_business_transaction_rollback(monkeypatch):
    """The telemetry write uses its own committed session, independent of the
    caller's session — so a later rollback in the caller's transaction must
    not lose the provider.call event.
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]], usage=_FakeUsage(prompt_tokens=1, total_tokens=1)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    async with _test_session_factory() as caller_session:
        from engram.db import apply_rls_context

        await apply_rls_context(caller_session, tenant_id=tenant_id, principal_id=tenant_id)
        await embeddings_mod.generate_embedding(
            "rollback test", tenant_id=tenant_id, operation="embedding_document"
        )
        # Simulate the caller's own business transaction failing/rolling back
        # AFTER the provider call already happened.
        await caller_session.rollback()

    calls = await _provider_calls("embedding_document")
    assert len(calls) == 1


async def test_telemetry_insertion_failure_does_not_fail_business_operation(monkeypatch):
    """A DB-level failure inside the telemetry insert itself (caught by
    record_usage_event_best_effort's own try/except) must never propagate to
    the caller — the embedding call must still succeed and return its vector.
    """
    if not await _db_ok():
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
    tenant_id = await _default_tenant_id()
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    vec_response = _FakeEmbeddingsResponse(
        vectors=[[0.1, 0.2, 0.3]], usage=_FakeUsage(prompt_tokens=1, total_tokens=1)
    )
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kw: FakeAsyncOpenAI(embeddings_response=vec_response)
    )

    async def _broken_rls_context(*args: object, **kwargs: object) -> None:
        raise RuntimeError("telemetry backend unavailable")

    import engram.db as db_module

    monkeypatch.setattr(db_module, "apply_rls_context", _broken_rls_context)

    # The embedding call itself must still succeed even though the telemetry
    # insert blew up inside record_usage_event_best_effort's own try/except.
    vector = await embeddings_mod.generate_embedding(
        "resilience test", tenant_id=tenant_id, operation="embedding_document"
    )
    assert vector == [0.1, 0.2, 0.3]

    calls = await _provider_calls("embedding_document")
    assert len(calls) == 0

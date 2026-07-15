"""Database-free retrieval embedding-stage telemetry tests (ENG-METER-002)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from engram.api.routes import memory as memory_routes
from engram.api.routes.memory import SearchRequest
from engram.usage import EmbeddingOutcome, embedding_call_occurred_for


async def _noop_record(*args: Any, **kwargs: Any) -> None:
    return None


async def _run_search(
    request: SearchRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, EmbeddingOutcome], Any]:
    stage: dict[str, EmbeddingOutcome] = {"value": "not_attempted"}
    result = await memory_routes._search_impl(
        request,
        object(),  # type: ignore[arg-type]
        uuid4(),
        uuid4(),
        object(),
        _noop_record,
        stage,
    )
    return stage, result


async def test_keyword_search_marks_embedding_not_required(monkeypatch):
    async def keyword(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(memory_routes, "_keyword_search", keyword)
    stage, _ = await _run_search(SearchRequest(query="keyword", mode="keyword"), monkeypatch)
    assert stage["value"] == "not_required"
    assert embedding_call_occurred_for(stage["value"]) is False


async def test_semantic_failure_before_embedding_is_not_attempted(monkeypatch):
    import engram.embedding_profiles as profiles

    async def fail_before_call(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("profile lookup failed")

    monkeypatch.setattr(profiles, "get_active_profile", fail_before_call)
    stage: dict[str, EmbeddingOutcome] = {"value": "not_attempted"}
    with pytest.raises(RuntimeError):
        await memory_routes._search_impl(
            SearchRequest(query="semantic", mode="semantic"),
            object(),  # type: ignore[arg-type]
            uuid4(),
            uuid4(),
            object(),
            _noop_record,
            stage,
        )
    assert stage["value"] == "not_attempted"
    assert embedding_call_occurred_for(stage["value"]) is False


async def test_semantic_disabled_embedding_is_disabled(monkeypatch):
    import engram.embedding_profiles as profiles

    async def active_profile(*args: Any, **kwargs: Any) -> object:
        return object()

    async def disabled(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_candidates(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(profiles, "get_active_profile", active_profile)
    monkeypatch.setattr(memory_routes, "generate_embedding", disabled)
    monkeypatch.setattr(memory_routes.semantic, "candidate_count", no_candidates)
    stage, _ = await _run_search(SearchRequest(query="semantic", mode="semantic"), monkeypatch)
    assert stage["value"] == "disabled"
    assert embedding_call_occurred_for(stage["value"]) is False


async def test_semantic_embedding_provider_exception_is_failed(monkeypatch):
    import engram.embedding_profiles as profiles

    async def active_profile(*args: Any, **kwargs: Any) -> object:
        return object()

    async def provider_failure(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("provider failed")

    monkeypatch.setattr(profiles, "get_active_profile", active_profile)
    monkeypatch.setattr(memory_routes, "generate_embedding", provider_failure)
    stage: dict[str, EmbeddingOutcome] = {"value": "not_attempted"}
    with pytest.raises(RuntimeError):
        await memory_routes._search_impl(
            SearchRequest(query="semantic", mode="semantic"),
            object(),  # type: ignore[arg-type]
            uuid4(),
            uuid4(),
            object(),
            _noop_record,
            stage,
        )
    assert stage["value"] == "failed"
    assert embedding_call_occurred_for(stage["value"]) is True


async def test_database_failure_after_embedding_stays_succeeded(monkeypatch):
    import engram.embedding_profiles as profiles

    async def active_profile(*args: Any, **kwargs: Any) -> object:
        return object()

    async def succeeded(*args: Any, **kwargs: Any) -> list[float]:
        return [1.0]

    async def database_failure(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("database failed")

    monkeypatch.setattr(profiles, "get_active_profile", active_profile)
    monkeypatch.setattr(memory_routes, "generate_embedding", succeeded)
    monkeypatch.setattr(memory_routes.semantic, "candidate_count", database_failure)
    stage: dict[str, EmbeddingOutcome] = {"value": "not_attempted"}
    with pytest.raises(RuntimeError):
        await memory_routes._search_impl(
            SearchRequest(query="semantic", mode="semantic"),
            object(),  # type: ignore[arg-type]
            uuid4(),
            uuid4(),
            object(),
            _noop_record,
            stage,
        )
    assert stage["value"] == "succeeded"
    assert embedding_call_occurred_for(stage["value"]) is True

from __future__ import annotations

import uuid

import pytest

from engram.embedding_profiles import (
    MAX_INDEX_DIMENSIONS,
    make_profile_key,
    profile_index_name,
    profile_index_sql,
    validate_profile,
)
from engram.models import EmbeddingProfile


def _profile(*, dimensions: int = 768) -> EmbeddingProfile:
    return EmbeddingProfile(
        id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        profile_key="openai:test/model:768;drop table memory_items",
        provider="openai",
        model="test/model",
        dimensions=dimensions,
        distance_metric="cosine",
        state="candidate",
        index_status="missing",
        profile_metadata={},
    )


def test_make_profile_key_is_deterministic() -> None:
    assert make_profile_key("openai", "text-embedding-x", 768) == (
        "openai:text-embedding-x:768"
    )


def test_index_name_is_sanitized_and_deterministic() -> None:
    profile = _profile()
    assert profile_index_name(profile) == profile_index_name(profile)
    assert profile_index_name(profile).startswith("idx_emb_profile_")
    assert "drop" not in profile_index_name(profile)


def test_index_sql_matches_profile_query_contract() -> None:
    profile = _profile()
    sql = profile_index_sql(profile, concurrently=True)
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_emb_profile_" in sql
    assert "(embedding::vector(768)) vector_cosine_ops" in sql
    assert f"profile_id = '{profile.id}'::uuid" in sql
    assert "embedding_dim = 768" in sql
    assert "embedding_status = 'ready'" in sql
    assert profile.profile_key not in sql
    assert profile.model not in sql


def test_unindexable_dimension_fails_clearly() -> None:
    with pytest.raises(ValueError, match="not indexable"):
        profile_index_sql(_profile(dimensions=MAX_INDEX_DIMENSIONS + 1))


@pytest.mark.parametrize("dimensions", [0, -1])
def test_invalid_dimensions_are_rejected(dimensions: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        validate_profile(_profile(dimensions=dimensions))

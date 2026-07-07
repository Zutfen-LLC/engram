"""Tests for startup recall — scoring, budgeting, pinning, determinism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from engram.models import MemoryItem, TenantConfig
from engram.recall import _enforce_budget, _separate_pinned, score_item


def _make_item(**overrides: Any) -> MemoryItem:
    """Create a MemoryItem with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "workspace_id": None,
        "principal_id": uuid4(),
        "content": "test memory content",
        "content_hash": "sha256:abc123",
        "kind": "fact",
        "visibility": "workspace",
        "review_status": "active",
        "memory_confidence": 0.5,
        "source_trust": 0.5,
        "human_verified": False,
        "verified_by": None,
        "verified_at": None,
        "importance": 0.5,
        "pinned": False,
        "last_recalled_at": None,
        "recall_count": 0,
        "startup_recall_count": 0,
        "last_verified_at": None,
        "source_type": "manual",
        "source_session": None,
        "source_uri": None,
        "extracted_by_model": None,
        "extraction_confidence": None,
        "conflicts_with_item_id": None,
        "conflict_type": None,
        "conflict_resolution_status": None,
        "conflict_resolved_by": None,
        "conflict_resolved_at": None,
        "sensitivity": "normal",
        "external_id": None,
        "external_source": None,
        "valid_from": datetime.now(UTC),
        "valid_to": None,
        "superseded_by": None,
        "created_at": datetime.now(UTC),
        "wing": None,
        "room": None,
        "subject_type": None,
        "subject_id": None,
        "subject_name": None,
    }
    defaults.update(overrides)
    return MemoryItem(**defaults)


def _make_config(**overrides: Any) -> TenantConfig:
    """Create a TenantConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "config_version": "v1",
        "weight_importance": 0.30,
        "weight_source_trust": 0.25,
        "weight_memory_confidence": 0.20,
        "weight_recency": 0.15,
        "weight_verified": 0.10,
        "auto_promote_enabled": True,
        "auto_promote_confidence_threshold": 0.7,
        "auto_promote_min_age_hours": 72,
        "max_pinned_tokens": 2048,
        "stale_after_days": 90,
        "startup_recall_penalty_threshold": 5,
        "startup_recall_penalty_factor": 0.5,
        "trust_manual_user": 0.9,
        "trust_manual_agent": 0.6,
        "trust_import": 0.8,
        "trust_extraction": 0.5,
        "trust_sync_turn": 0.4,
        "trust_pre_compress": 0.3,
        "confidence_manual_user": 0.9,
        "confidence_manual_agent": 0.5,
        "confidence_import": 0.8,
        "confidence_extraction": 0.5,
        "confidence_sync_turn": 0.4,
        "confidence_pre_compress": 0.3,
        "active": True,
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return TenantConfig(**defaults)


class TestScoreItem:
    def test_basic_score(self) -> None:
        """Score formula correctness."""
        item = _make_item(
            importance=0.9,
            source_trust=0.8,
            memory_confidence=0.7,
            human_verified=True,
            last_recalled_at=datetime.now(UTC) - timedelta(days=5),
        )
        config = _make_config()
        now = datetime.now(UTC)
        result = score_item(item, config, now)
        assert result.score > 0.0
        assert "human_verified" in result.reasons

    def test_unverified_no_bonus(self) -> None:
        item = _make_item(human_verified=False)
        config = _make_config()
        result = score_item(item, config, datetime.now(UTC))
        assert "human_verified" not in result.reasons

    def test_no_recency_when_never_recalled(self) -> None:
        item = _make_item(last_recalled_at=None)
        config = _make_config()
        result = score_item(item, config, datetime.now(UTC))
        assert any("recency=0" in r for r in result.reasons)


class TestPinnedBypass:
    def test_pinned_separated(self) -> None:
        items = [
            _make_item(pinned=True),
            _make_item(pinned=False),
            _make_item(pinned=True),
        ]
        pinned, scored, omitted = _separate_pinned(items, max_pinned_tokens=99999)
        assert len(pinned) == 2
        assert len(scored) == 1
        assert omitted == 0


class TestBudgetEnforcement:
    def test_byte_budget(self) -> None:
        items_with_scores = [
            (_make_item(content="a" * 100), 0.9),
            (_make_item(content="b" * 100), 0.8),
        ]
        result = _enforce_budget(items_with_scores, byte_budget=150, token_budget=None)
        total_bytes = sum(len(i.content.encode()) for i, _ in result)
        assert total_bytes <= 150


class TestDeterminism:
    def test_same_corpus_same_output(self) -> None:
        """Same items + same config = same scores."""
        items = [_make_item(importance=0.7), _make_item(importance=0.3)]
        config = _make_config()
        now = datetime.now(UTC)
        results1 = [score_item(i, config, now) for i in items]
        results2 = [score_item(i, config, now) for i in items]
        assert [r.score for r in results1] == [r.score for r in results2]

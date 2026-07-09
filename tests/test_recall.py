"""Tests for startup recall — scoring, budgeting, pinning, determinism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from engram.models import MemoryItem, TenantConfig
from engram.recall import (
    _enforce_budget,
    _enforce_semantic_budget,
    _resolve_recall_budgets,
    _separate_pinned,
    score_item,
)


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

    def test_freshness_recency_for_never_recalled(self) -> None:
        """A never-recalled fresh item still gets a recency contribution via
        the freshness signal (decay from valid_from), so recall is not biased
        purely toward previously-recalled incumbents."""
        item = _make_item(last_recalled_at=None)
        config = _make_config()
        now = datetime.now(UTC)
        result = score_item(item, config, now)
        # Freshness reason is present and recency is non-zero for a just-created item.
        assert any("freshness=" in r for r in result.reasons)
        assert not any("recency=0.00" in r for r in result.reasons)

    def test_freshness_half_weighted_and_deterministic(self) -> None:
        """Freshness is capped at 0.5 (half-weight) and deterministic under a
        frozen clock."""
        now = datetime.now(UTC)
        item = _make_item(last_recalled_at=None, valid_from=now, created_at=now)
        config = _make_config()
        r1 = score_item(item, config, now)
        r2 = score_item(item, config, now)
        assert r1.score == r2.score
        # A brand-new item has freshness = (1 - 0/30) * 0.5 = 0.5.
        assert any("freshness=0.50" in reason for reason in r1.reasons)

    def test_freshness_does_not_dominate_trust(self) -> None:
        """An old, high-trust item should still outscore a fresh, low-trust
        item (freshness must not dominate trust/importance)."""
        now = datetime.now(UTC)
        fresh_low_trust = _make_item(
            last_recalled_at=None,
            valid_from=now,
            created_at=now,
            importance=0.1,
            source_trust=0.1,
            memory_confidence=0.1,
        )
        old_high_trust = _make_item(
            last_recalled_at=now - timedelta(days=100),
            valid_from=now - timedelta(days=200),
            created_at=now - timedelta(days=200),
            importance=0.9,
            source_trust=0.9,
            memory_confidence=0.9,
            human_verified=True,
        )
        config = _make_config()
        fresh_score = score_item(fresh_low_trust, config, now).score
        old_score = score_item(old_high_trust, config, now).score
        assert old_score > fresh_score

    def test_anti_feedback_penalty_only_affects_recall_recency(self) -> None:
        """The startup anti-feedback penalty reduces recall-driven recency but
        a fresh, never-recalled item still gets its full freshness signal."""
        now = datetime.now(UTC)
        # A frequently-recalled item: high startup_recall_count triggers the
        # penalty, which must NOT bleed into a separate fresh item's freshness.
        penalized = _make_item(
            last_recalled_at=now - timedelta(days=1),
            startup_recall_count=20,
        )
        config = _make_config()
        result = score_item(penalized, config, now)
        # The penalty reason is present and tied to recall recency.
        assert any("recency_penalty(count=20)" in r for r in result.reasons)
        # A never-recalled fresh item shows freshness with no penalty reason.
        fresh = _make_item(last_recalled_at=None, valid_from=now, created_at=now)
        fresh_result = score_item(fresh, config, now)
        assert not any("recency_penalty" in r for r in fresh_result.reasons)
        assert any("freshness=" in r for r in fresh_result.reasons)


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

    def test_skip_not_break_byte_budget(self) -> None:
        """An oversized high-ranked item is skipped and lower-ranked items
        that fit still fill the budget (requirement: skip-not-break)."""
        items_with_scores = [
            (_make_item(content="A" * 1200), 0.99),  # oversized -> skipped
            (_make_item(content="B" * 200), 0.90),  # fits
            (_make_item(content="C" * 300), 0.80),  # fits
        ]
        result = _enforce_budget(items_with_scores, byte_budget=1000, token_budget=None)
        contents = [i.content[0] for i, _ in result]
        # A omitted; B and C included, in rank order.
        assert contents == ["B", "C"]
        total_bytes = sum(len(i.content.encode()) for i, _ in result)
        assert total_bytes <= 1000

    def test_skip_not_break_token_budget(self) -> None:
        items_with_scores = [
            (_make_item(content="A" * 1200), 0.99),  # ~300 tokens -> skipped
            (_make_item(content="B" * 200), 0.90),  # ~50 tokens -> fits
            (_make_item(content="C" * 300), 0.80),  # ~75 tokens -> fits
        ]
        result = _enforce_budget(items_with_scores, byte_budget=None, token_budget=200)
        contents = [i.content[0] for i, _ in result]
        assert contents == ["B", "C"]


class TestSemanticBudgetSkipNotBreak:
    def test_oversized_top_item_skipped(self) -> None:
        candidates = [
            {"content": "A" * 1200},  # oversized, score-best
            {"content": "B" * 200},
            {"content": "C" * 300},
        ]
        result = _enforce_semantic_budget(
            candidates, byte_budget=1000, token_budget=None, item_budget=None
        )
        contents = [c["content"][0] for c in result]
        assert contents == ["B", "C"]

    def test_item_budget_cap_respected(self) -> None:
        candidates = [{"content": "x"}, {"content": "y"}, {"content": "z"}]
        result = _enforce_semantic_budget(
            candidates, byte_budget=None, token_budget=None, item_budget=2
        )
        assert len(result) == 2

    def test_no_budget_returns_all(self) -> None:
        candidates = [{"content": "a"}, {"content": "b"}]
        result = _enforce_semantic_budget(
            candidates, byte_budget=None, token_budget=None, item_budget=None
        )
        assert result == candidates


class TestDefaultBudgets:
    def test_omitted_budgets_use_global_defaults(self) -> None:
        byte_b, token_b, item_b = _resolve_recall_budgets(
            byte_budget=None, token_budget=None, item_budget=None
        )
        from engram.config import settings

        assert byte_b == settings.recall_byte_budget
        assert item_b == settings.recall_item_budget
        assert token_b is None  # no global default

    def test_explicit_budgets_override_defaults(self) -> None:
        byte_b, token_b, item_b = _resolve_recall_budgets(
            byte_budget=128, token_budget=64, item_budget=3
        )
        assert byte_b == 128
        assert token_b == 64
        assert item_b == 3

    def test_recall_is_bounded_by_default(self) -> None:
        """Omitted byte/item budgets resolve to non-None defaults."""
        byte_b, _token_b, item_b = _resolve_recall_budgets(
            byte_budget=None, token_budget=None, item_budget=None
        )
        assert byte_b is not None
        assert item_b is not None



class TestDeterminism:
    def test_same_corpus_same_output(self) -> None:
        """Same items + same config = same scores."""
        items = [_make_item(importance=0.7), _make_item(importance=0.3)]
        config = _make_config()
        now = datetime.now(UTC)
        results1 = [score_item(i, config, now) for i in items]
        results2 = [score_item(i, config, now) for i in items]
        assert [r.score for r in results1] == [r.score for r in results2]

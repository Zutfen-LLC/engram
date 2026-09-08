"""Migration contract for explicit candidate priority (issue #196)."""

from pathlib import Path


def test_explicit_priority_migration_snapshots_legacy_importance_without_reconstruction() -> None:
    sql = Path("migrations/043_explicit_priority.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS explicit_priority REAL" in sql
    assert "SET explicit_priority = importance" in sql
    assert "trg_memory_items_default_explicit_priority" in sql
    assert "feedback_events" not in sql

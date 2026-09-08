"""Static migration contract for semantic Context Receipts."""

from pathlib import Path

SQL_PATH = Path("migrations/042_semantic_context_receipts.sql")
DOWN_PATH = Path("migrations/downgrades/042_semantic_context_receipts.sql")
SQL = SQL_PATH.read_text()
DOWN = DOWN_PATH.read_text()


def test_migration_is_additive_and_reapplicable() -> None:
    assert "ADD COLUMN IF NOT EXISTS item_budget" in SQL
    assert SQL.count("DROP CONSTRAINT IF EXISTS") >= 3
    assert "DELETE FROM context_receipts" not in SQL
    assert "UPDATE context_receipts" not in SQL


def test_schema_mode_pairs_are_closed() -> None:
    assert "chk_context_receipts_schema_mode_pair" in SQL
    assert "manifest_schema = 'engram.context-manifest' AND mode = 'startup'" in SQL
    assert (
        "manifest_schema = 'engram.semantic-context-manifest' AND mode = 'semantic'"
        in SQL
    )


def test_downgrade_documents_forward_only_widening() -> None:
    assert "forward-only schema widening" in DOWN
    assert "DROP COLUMN IF EXISTS item_budget" in DOWN
    assert "context_receipts" not in DOWN.splitlines()[-1]

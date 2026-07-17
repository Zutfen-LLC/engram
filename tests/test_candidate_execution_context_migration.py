from __future__ import annotations

from pathlib import Path

SQL = Path("migrations/025_candidate_execution_context.sql").read_text()


def test_execution_context_is_separate_immutable_and_tenant_scoped() -> None:
    assert "CREATE TABLE IF NOT EXISTS candidate_ingest_executions" in SQL
    assert "REFERENCES candidate_ingests (tenant_id, id) ON DELETE CASCADE" in SQL
    assert "fk_candidate_ingest_executions_profile_revision" in SQL
    assert "REFERENCES memory_profile_revisions (id, profile_id, tenant_id)" in SQL
    assert "FORCE ROW LEVEL SECURITY" in SQL
    assert "GRANT SELECT, INSERT ON candidate_ingest_executions TO engram_app" in SQL
    assert "REVOKE UPDATE, DELETE ON candidate_ingest_executions FROM engram_app" in SQL


def test_execution_context_migration_is_idempotent() -> None:
    assert "IF NOT EXISTS" in SQL
    assert "CREATE INDEX IF NOT EXISTS" in SQL
    assert "SELECT 1 FROM pg_constraint" in SQL
    assert "SELECT 1 FROM pg_policies" in SQL

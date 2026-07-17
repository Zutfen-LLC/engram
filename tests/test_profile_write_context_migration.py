from __future__ import annotations

from pathlib import Path

SQL = Path("migrations/024_profile_write_context.sql").read_text()


def test_migration_adds_truthful_candidate_context_provenance() -> None:
    assert "ALTER TABLE candidate_ingests" in SQL
    for column in (
        "api_key_id",
        "memory_profile_id",
        "memory_profile_revision_id",
        "memory_context_version",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in SQL
    assert "legacy-unprofiled-v0" in SQL
    assert "chk_candidate_ingests_memory_profile_pair" in SQL
    assert "fk_candidate_ingests_memory_profile_revision" in SQL
    assert "REFERENCES memory_profile_revisions (id, profile_id, tenant_id)" in SQL


def test_migration_tenant_scopes_item_event_provenance() -> None:
    assert "ALTER TABLE item_events" in SQL
    assert "SET tenant_id = item.tenant_id" in SQL
    assert "ALTER COLUMN tenant_id SET NOT NULL" in SQL
    assert "fk_item_events_tenant_item" in SQL
    assert "FOREIGN KEY (item_id, tenant_id)" in SQL
    assert "fk_item_events_memory_profile_revision" in SQL
    assert "chk_item_events_memory_profile_pair" in SQL


def test_migration_is_additive_idempotent_and_preserves_rls() -> None:
    assert "ADD COLUMN IF NOT EXISTS" in SQL
    assert "IF NOT EXISTS (" in SQL
    assert "CREATE INDEX IF NOT EXISTS" in SQL
    assert "ALTER TABLE candidate_ingests FORCE ROW LEVEL SECURITY" in SQL
    assert "ALTER TABLE item_events FORCE ROW LEVEL SECURITY" in SQL
    assert "REVOKE UPDATE, DELETE ON candidate_ingests FROM engram_app" in SQL

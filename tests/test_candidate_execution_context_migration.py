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


def test_execution_context_relational_contract_is_tenant_safe() -> None:
    # tenant_id references tenants(id) so an execution row cannot outlive its tenant.
    assert "fk_candidate_ingest_executions_tenant" in SQL
    assert "REFERENCES tenants (id) ON DELETE CASCADE" in SQL
    # principal_id is tenant-scoped via the composite FK to principals(tenant_id, id),
    # matching candidate_ingests — a tenant-A row cannot reference a tenant-B principal.
    assert "fk_candidate_ingest_executions_tenant_principal" in SQL
    assert "REFERENCES principals (tenant_id, id) ON DELETE CASCADE" in SQL
    # The legacy single-column principal FK is removed so the composite one is authoritative.
    assert "DROP CONSTRAINT IF EXISTS candidate_ingest_executions_principal_id_fkey" in SQL
    # A profile pair is either both set or both null.
    assert "chk_candidate_ingest_executions_profile_pair" in SQL


def test_execution_context_rls_policy_uses_safe_tenant_setting() -> None:
    # The two-argument current_setting returns NULL (not an error) when the GUC
    # is unset, matching the tenant_isolation convention used across the schema.
    assert "current_setting('app.tenant_id', true)" in SQL
    # The single-argument form (which raises when unset) must not remain.
    assert "current_setting('app.tenant_id')::uuid" not in SQL


def test_execution_context_migration_is_idempotent() -> None:
    assert "IF NOT EXISTS" in SQL
    assert "CREATE INDEX IF NOT EXISTS" in SQL
    assert "SELECT 1 FROM pg_constraint" in SQL
    assert "SELECT 1 FROM pg_policies" in SQL
    # The policy is recreated idempotently (drop-if-exists + create) so reapplying
    # the migration after the policy normalization is safe.
    assert "DROP POLICY tenant_isolation_candidate_ingest_executions" in SQL

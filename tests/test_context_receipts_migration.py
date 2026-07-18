"""Static contract tests for migration 026 (context_receipts).

These supplement — but do not replace — the real-PostgreSQL migration tests in
``test_context_receipts_postgres.py``. They assert the migration text encodes
the required schema, constraints, RLS policy, privileges, indexes, and
idempotency guards so a future edit cannot silently weaken the substrate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SQL_PATH = Path("migrations/026_context_receipts.sql")
SQL = SQL_PATH.read_text()


def _ddl() -> str:
    """SQL with single-line ``--`` comments stripped, for DDL-only assertions."""
    return "\n".join(
        re.sub(r"--.*$", "", line) for line in SQL.splitlines()
    )


def test_migration_026_file_exists() -> None:
    assert SQL_PATH.exists()


def test_context_receipts_table_created() -> None:
    assert "CREATE TABLE IF NOT EXISTS context_receipts" in SQL


def test_required_columns_present() -> None:
    for column in (
        "id",
        "tenant_id",
        "principal_id",
        "recall_log_id",
        "manifest_schema",
        "manifest_schema_version",
        "canonicalization",
        "mode",
        "manifest",
        "manifest_hash",
        "packet_hash",
        "retention_expires_at",
        "created_at",
    ):
        assert column in SQL, f"missing column: {column}"


def test_no_raw_content_or_working_set_columns() -> None:
    # The manifest JSONB is the only served-context representation. No raw
    # working_set, no item content column, no semantic query text, no excluded
    # candidate IDs, no manifest_hash inside the manifest.
    ddl = _ddl()
    assert "working_set" not in ddl
    assert "query_text" not in ddl
    assert "excluded_candidate" not in ddl


def test_manifest_is_jsonb() -> None:
    assert re.search(r"\bmanifest\s+JSONB\s+NOT\s+NULL\b", _ddl()) is not None


def test_envelope_protocol_checks() -> None:
    assert "manifest_schema = 'engram.context-manifest'" in SQL
    assert "manifest_schema_version = '1.0'" in SQL
    assert "canonicalization = 'rfc8785'" in SQL
    assert "mode = 'startup'" in SQL


def test_hash_format_checks() -> None:
    assert "manifest_hash ~ '^sha256:[0-9a-f]{64}$'" in SQL
    assert "packet_hash ~ '^sha256:[0-9a-f]{64}$'" in SQL


def test_manifest_jsonb_shape_checks() -> None:
    assert "jsonb_typeof(manifest) = 'object'" in SQL
    for section in ("subject", "request", "versions", "result", "packet", "items"):
        assert f"jsonb_typeof(manifest -> '{section}')" in SQL, (
            f"missing manifest section check: {section}"
        )
    assert "jsonb_typeof(manifest -> 'items') = 'array'" in SQL


def test_envelope_manifest_agreement_checks() -> None:
    assert "manifest ->> 'schema' = manifest_schema" in SQL
    assert "manifest ->> 'schema_version' = manifest_schema_version" in SQL
    assert "manifest ->> 'canonicalization' = canonicalization" in SQL
    assert "manifest ->> 'mode' = mode" in SQL
    assert "manifest -> 'subject' ->> 'tenant_id' = tenant_id::text" in SQL
    assert "manifest -> 'subject' ->> 'principal_id' = principal_id::text" in SQL
    assert "manifest -> 'packet' ->> 'hash' = packet_hash" in SQL


def test_no_manifest_hash_field_inside_manifest() -> None:
    assert "NOT (manifest ? 'manifest_hash')" in SQL


def test_retention_metadata_check() -> None:
    assert (
        "retention_expires_at IS NULL OR retention_expires_at >= created_at" in SQL
    )


def test_one_to_one_recall_log_unique_identity() -> None:
    assert "uq_recall_logs_tenant_principal_id" in SQL
    assert "UNIQUE (tenant_id, principal_id, id)" in SQL


def test_composite_foreign_key_to_recall_log() -> None:
    assert "fk_context_receipts_recall_log" in SQL
    assert "FOREIGN KEY (tenant_id, principal_id, recall_log_id)" in SQL
    assert "REFERENCES recall_logs (tenant_id, principal_id, id)" in SQL
    assert "ON DELETE RESTRICT" in SQL
    assert "DEFERRABLE INITIALLY DEFERRED" in SQL


def test_force_rls_enabled() -> None:
    assert "ALTER TABLE context_receipts ENABLE ROW LEVEL SECURITY" in SQL
    assert "ALTER TABLE context_receipts FORCE ROW LEVEL SECURITY" in SQL


def test_rls_policy_requires_both_tenant_and_principal() -> None:
    assert "tenant_principal_isolation_context_receipts" in SQL
    assert "current_setting('app.tenant_id', true)" in SQL
    assert "current_setting('app.principal_id', true)" in SQL
    # USING and WITH CHECK use the same expression.
    assert SQL.count("tenant_id::text = current_setting('app.tenant_id', true)") >= 2
    assert (
        SQL.count("principal_id::text = current_setting('app.principal_id', true)")
        >= 2
    )
    # The single-argument form (which raises when unset) must not be present.
    # Match the closing paren immediately after the setting name (no second arg).
    assert not re.search(r"current_setting\('app\.tenant_id'\)", SQL)
    assert not re.search(r"current_setting\('app\.principal_id'\)", SQL)


def test_app_role_select_insert_only() -> None:
    assert "GRANT SELECT, INSERT ON context_receipts TO engram_app" in SQL
    assert "REVOKE UPDATE, DELETE ON context_receipts FROM engram_app" in SQL


def test_required_indexes() -> None:
    assert "idx_context_receipts_recall_log" in SQL
    assert "ON context_receipts (recall_log_id)" in SQL
    assert "UNIQUE INDEX IF NOT EXISTS idx_context_receipts_recall_log" in SQL
    assert "idx_context_receipts_principal_timeline" in SQL
    assert "ON context_receipts (tenant_id, principal_id, created_at DESC)" in SQL
    assert "idx_context_receipts_tenant_manifest_hash" in SQL
    assert "ON context_receipts (tenant_id, manifest_hash)" in SQL
    assert "idx_context_receipts_retention_sweep" in SQL
    assert "ON context_receipts (retention_expires_at)" in SQL
    assert "WHERE retention_expires_at IS NOT NULL" in SQL


def test_no_gin_index_over_manifest() -> None:
    # ENG-CONTEXT-003 adds query-specific indexes after its API shapes are known.
    # Match "GIN" as an index type keyword, not the substring of "BEGIN".
    assert re.search(r"\bGIN\b", _ddl()) is None


def test_migration_is_idempotent() -> None:
    assert "CREATE TABLE IF NOT EXISTS" in SQL
    assert "CREATE INDEX IF NOT EXISTS" in SQL
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in SQL
    assert "SELECT 1 FROM pg_constraint" in SQL
    assert "SELECT 1 FROM pg_policies" in SQL
    # Policy is recreated idempotently (drop-if-exists + create).
    assert "DROP POLICY tenant_principal_isolation_context_receipts" in SQL


def test_migration_does_not_modify_prior_migrations() -> None:
    # Only recall_logs is touched (to add the unique identity), and only via a
    # guarded ADD CONSTRAINT. No ALTER TABLE on other tables.
    assert "ALTER TABLE recall_logs" in SQL
    # The migration must not rewrite rows or weaken privileges on other tables.
    assert "UPDATE recall_logs" not in SQL
    assert "DELETE FROM recall_logs" not in SQL


def test_migration_order_note() -> None:
    # The migration filename sorts after 025 so it applies last in order.
    assert SQL_PATH.name == "026_context_receipts.sql"


def test_principals_tenant_identity_reasserted() -> None:
    assert "idx_principals_tenant_identity" in SQL


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
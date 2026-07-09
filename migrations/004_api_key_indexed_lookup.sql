-- Engram — O(1) API-key lookup (ENG-AUD-003)
-- 004_api_key_indexed_lookup.sql
--
-- Audit finding: API-key verification performed an O(n·bcrypt) scan over every
-- non-revoked api_keys row on each authenticated request. bcrypt is
-- intentionally slow, so this path becomes a hosted-service blocker as
-- tenants/keys grow, and it requires broad owner-role reads because the caller's
-- tenant is unknown until after key resolution.
--
-- This migration adds indexed, O(1) key lookup for keys created from this
-- point forward. New keys are ``eng_<key_id>_<secret>``: the ``key_id`` is
-- looked up by a unique partial index, and the high-entropy ``secret`` is
-- verified against a fast deterministic digest (sha256) with a constant-time
-- comparison. Existing bcrypt-only keys CANNOT be backfilled (their plaintext
-- secret is unavailable), so they keep working through a transitional legacy
-- fallback path (the old bcrypt scan, scoped to rows where key_id IS NULL).
--
-- Run as: psql -f migrations/004_api_key_indexed_lookup.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. Columns for indexed lookup ============

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_id TEXT;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS secret_digest TEXT;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS digest_algorithm TEXT;

-- New-format keys store only a digest of the secret; the bcrypt ``key_hash`` is
-- no longer required for them. DROP NOT NULL so new rows can omit it (legacy
-- rows keep their bcrypt hash and remain valid through the fallback path).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'api_keys'
          AND column_name = 'key_hash'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE api_keys ALTER COLUMN key_hash DROP NOT NULL;
    END IF;
END
$$;

-- ============ 2. Unique partial index on key_id ============
-- New-format keys carry a globally-unique, random key_id, so an O(1) lookup by
-- key_id resolves the row (and its tenant/principal/scopes) without a scan.
-- ``key_id IS NOT NULL`` keeps the index small (legacy rows are excluded) and
-- lets multiple legacy rows (key_id NULL) coexist. Unique over all non-NULL
-- key_ids — key_ids are random and never reused, including after revocation.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_apikeys_keyid'
    ) THEN
        CREATE UNIQUE INDEX idx_apikeys_keyid ON api_keys (key_id) WHERE key_id IS NOT NULL;
    END IF;
END
$$;

-- No privilege/RLS changes: the new columns belong to an existing table, and a
-- table-level GRANT (migration 003 grants SELECT/INSERT/UPDATE/DELETE on all
-- api_keys columns, present and future) already covers them. Key resolution
-- reads through the owner role (which bypasses RLS) so a key_id lookup sees the
-- row regardless of tenant before the resolved tenant is applied to the request
-- session — unchanged from the prior scan-based resolution.

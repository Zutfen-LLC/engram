-- Engram — Application role + FORCE ROW LEVEL SECURITY (ENG-AUD-002)
-- 003_app_role_and_force_rls.sql
--
-- Audit finding F3: RLS was ENABLEd but inert in the shipped deployment because
-- the service connected as the table owner (Postgres table owners bypass RLS
-- unless FORCE ROW LEVEL SECURITY is set). This migration:
--
--   1. Creates a dedicated non-owner application role (``engram_app``) with
--      only the privileges normal service operation needs. It owns no tables
--      and has no BYPASSRLS, so RLS policies apply to it.
--   2. Grants that role schema usage + DML on all current tables and
--      auto-grants future tables created by the owner role.
--   3. Forces ROW LEVEL SECURITY on every tenant-scoped table, so isolation
--      holds even if the connecting role is the table owner (defense in depth;
--      makes single-role deployments safe).
--
-- Runtime connects as ``engram_app``; migrations/admin run as the table-owning
-- role (a superuser in the default Compose image, which still bypasses RLS so
-- cross-tenant CLI paths keep working).
--
-- Run as: psql -f migrations/003_app_role_and_force_rls.sql  (owner/migration role)

-- ============ 1. Non-owner application role ============
-- Created WITHOUT a password here: a role with no password cannot authenticate,
-- so this is safe. The deployable password is set out-of-band (see
-- migrations/app_role_password.sh for first-boot, or an explicit
-- ``ALTER ROLE engram_app WITH PASSWORD '...'`` for upgrades).
-- ``NOBYPASSRLS`` is the default for a non-superuser role, but stated explicitly
-- so the security posture is unambiguous in the schema.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_app') THEN
        CREATE ROLE engram_app LOGIN NOBYPASSRLS NOCREATEDB NOCREATEROLE NOSUPERUSER;
    END IF;
END
$$;

-- ============ 2. Privileges for the application role ============
-- Only what normal service operation requires: schema USAGE and DML on tables.
-- No table ownership, no superuser, no BYPASSRLS, no DDL (CREATE).
GRANT USAGE ON SCHEMA public TO engram_app;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public TO engram_app;

-- Revoke the blanket grant on tables that are NOT RLS-protected. ``tenants``
-- is the root parent (intentionally no policy — every tenant's name/slug would
-- otherwise be readable cross-tenant, and tenant rows could be tampered with);
-- ``schema_migrations`` is migration bookkeeping. The runtime app role must not
-- reach either. (Principal/tenant resolution runs through the OWNER role, so the
-- app role needs no privilege on ``tenants``.)
--
-- ``schema_migrations`` may not exist yet on a first-boot initdb.d run (it is
-- created lazily by ``engram init-db``, not by any SQL migration), and the
-- GRANT ... ON ALL TABLES above only covered tables that already existed — so no
-- grant touched it in that case. Guard its revocation so the migration does not
-- hard-fail; ``tenants`` always exists after 001 and is revoked unconditionally.
REVOKE ALL PRIVILEGES ON tenants FROM engram_app;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
    ) THEN
        EXECUTE 'REVOKE ALL PRIVILEGES ON schema_migrations FROM engram_app';
    END IF;
END
$$;

-- Sequence USAGE for any SERIAL/identity columns (none today — every key is a
-- uuid_generate_v4() default — but granted for forward-safety).
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO engram_app;

-- Auto-grant DML on tables the owner creates in future migrations, so a new
-- tenant-scoped table is usable by the app role without a per-migration grant.
ALTER DEFAULT PRIVILEGES FOR ROLE engram IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO engram_app;
ALTER DEFAULT PRIVILEGES FOR ROLE engram IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO engram_app;

-- ============ 3. FORCE ROW LEVEL SECURITY ============
-- ENABLE ROW LEVEL SECURITY alone does not apply policies to the table owner.
-- FORCE makes policies apply to the owner too, so tenant isolation holds
-- regardless of which role connects (defense in depth). Superusers still bypass
-- RLS by design, so the migration/admin role (a superuser in Compose) can run
-- cross-tenant CLI/admin paths.
--
-- Applied to every table that carries a tenant_isolation_* policy (see
-- 001_init.sql). ``tenants`` (the root parent, no policy) and
-- ``schema_migrations`` (meta, no tenant data) are intentionally NOT forced.

ALTER TABLE memory_items       FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings  FORCE ROW LEVEL SECURITY;
ALTER TABLE kg_triples         FORCE ROW LEVEL SECURITY;
ALTER TABLE tunnels            FORCE ROW LEVEL SECURITY;
ALTER TABLE item_events        FORCE ROW LEVEL SECURITY;
ALTER TABLE classification_rules FORCE ROW LEVEL SECURITY;
ALTER TABLE recall_logs        FORCE ROW LEVEL SECURITY;
ALTER TABLE workspace_members  FORCE ROW LEVEL SECURITY;
ALTER TABLE api_keys           FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_config      FORCE ROW LEVEL SECURITY;
ALTER TABLE deletion_events    FORCE ROW LEVEL SECURITY;
ALTER TABLE feedback_events    FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces         FORCE ROW LEVEL SECURITY;
ALTER TABLE principals         FORCE ROW LEVEL SECURITY;

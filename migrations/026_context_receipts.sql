-- Engram — Durable Context Receipt storage substrate (ENG-CONTEXT-002A)
-- 026_context_receipts.sql
--
-- This migration introduces the immutable, tenant/principal-isolated
-- ``context_receipts`` table: the volatile persistence envelope for the
-- deterministic ``ContextManifestV1`` (ENG-CONTEXT-001).
--
-- The manifest is the content-addressed, RFC 8785-canonicalized artifact
-- (manifest_hash). The receipt is its database envelope: receipt ID, recall-log
-- identity, tenant/principal ownership, creation time, retention metadata, the
-- stored JSONB manifest, manifest_hash, and packet_hash. Receipt ID, creation
-- time, recall-log ID, and retention metadata are deliberately OUTSIDE the
-- manifest hash.
--
-- Scope of THIS slice (ENG-CONTEXT-002A): storage only. No production recall
-- path writes a receipt yet (dark writes belong to ENG-CONTEXT-002B). The table
-- is additive; rolling application code back leaves an unused table.
--
-- Run as: psql -f migrations/026_context_receipts.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. Unique recall-log identity ============
-- A receipt must belong to exactly one recall log and cannot disagree with that
-- log about tenant or principal ownership. Add a composite unique identity on
-- recall_logs so a composite foreign key can pin all three columns together
-- (three independent FKs would allow the ownership columns to describe a
-- different parent row). Idempotent. Also re-assert the principals tenant
-- identity candidate key (introduced by migration 020) so a database
-- provisioned from an older/custom baseline still has the exact key the
-- principal FK below requires.

CREATE UNIQUE INDEX IF NOT EXISTS idx_principals_tenant_identity
    ON principals (tenant_id, id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_recall_logs_tenant_principal_id'
          AND conrelid = 'recall_logs'::regclass
    ) THEN
        ALTER TABLE recall_logs
            ADD CONSTRAINT uq_recall_logs_tenant_principal_id
            UNIQUE (tenant_id, principal_id, id);
    END IF;
END
$$;

-- ============ 2. Table ============

CREATE TABLE IF NOT EXISTS context_receipts (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id                   UUID NOT NULL,
    principal_id                UUID NOT NULL,
    recall_log_id               UUID NOT NULL,

    -- Envelope protocol markers (mirrored in the JSONB manifest and CHECK'd to
    -- agree with it). These are stored as columns so they are indexable and so
    -- the database can reject an obviously invalid envelope without parsing JSON.
    manifest_schema             TEXT NOT NULL,
    manifest_schema_version     TEXT NOT NULL,
    canonicalization            TEXT NOT NULL,
    mode                        TEXT NOT NULL,

    -- The deterministic manifest artifact (JSONB object). Verification parses
    -- this back through ContextManifestV1, recanonicalizes under RFC 8785, and
    -- compares the recomputed hash to manifest_hash. No raw memory content, no
    -- raw working_set, no raw query text, and no manifest_hash field inside.
    manifest                    JSONB NOT NULL,

    -- Content-addressed identity of the stored manifest (SHA-256 of the RFC 8785
    -- canonical bytes) and of the served packet. Never caller-supplied in
    -- contradiction to the manifest: the repository computes manifest_hash from
    -- the manifest and reads packet_hash from manifest.packet.hash.
    manifest_hash               TEXT NOT NULL,
    packet_hash                 TEXT NOT NULL,

    -- Retention metadata only in this slice. NULL means no expiry has been
    -- assigned; a non-NULL value is the earliest time at which a future
    -- retention process MAY evaluate the receipt for deletion. This slice does
    -- not delete expired rows and does not change retention after insert.
    retention_expires_at        TIMESTAMPTZ NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Protocol CHECK constraints: the database rejects obviously invalid
    -- envelopes. Exact manifest-hash verification (RFC 8785 re-canonicalization)
    -- belongs in the Python receipt verifier — PostgreSQL does not implement a
    -- second JSON canonicalizer.
    CONSTRAINT chk_context_receipts_schema CHECK (
        manifest_schema = 'engram.context-manifest'
    ),
    CONSTRAINT chk_context_receipts_schema_version CHECK (
        manifest_schema_version = '1.0'
    ),
    CONSTRAINT chk_context_receipts_canonicalization CHECK (
        canonicalization = 'rfc8785'
    ),
    CONSTRAINT chk_context_receipts_mode CHECK (
        mode = 'startup'
    ),
    CONSTRAINT chk_context_receipts_manifest_hash CHECK (
        manifest_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_context_receipts_packet_hash CHECK (
        packet_hash ~ '^sha256:[0-9a-f]{64}$'
    ),

    -- The manifest JSONB is an object with the required top-level sections.
    -- JSON accessors (->) return NULL when a key is absent, and a CHECK passes
    -- when its expression is TRUE OR NULL. The JSONB-typed checks therefore
    -- wrap each comparison in ``IS TRUE`` so an absent section rejects the row
    -- instead of silently passing as NULL.
    CONSTRAINT chk_context_receipts_manifest_is_object CHECK (
        jsonb_typeof(manifest) = 'object'
    ),
    CONSTRAINT chk_context_receipts_manifest_sections CHECK (
        (jsonb_typeof(manifest -> 'subject') = 'object') IS TRUE
        AND (jsonb_typeof(manifest -> 'request') = 'object') IS TRUE
        AND (jsonb_typeof(manifest -> 'versions') = 'object') IS TRUE
        AND (jsonb_typeof(manifest -> 'result') = 'object') IS TRUE
        AND (jsonb_typeof(manifest -> 'packet') = 'object') IS TRUE
        AND (jsonb_typeof(manifest -> 'items') = 'array') IS TRUE
    ),

    -- Envelope/column agreement: the mirrored protocol markers inside the
    -- JSONB manifest must equal the envelope columns. ``->>`` returns NULL
    -- when the key is absent, so each equality is wrapped in ``IS TRUE`` to
    -- reject a missing marker rather than treating it as a NULL-pass.
    CONSTRAINT chk_context_receipts_schema_agreement CHECK (
        (manifest ->> 'schema' = manifest_schema) IS TRUE
        AND (manifest ->> 'schema_version' = manifest_schema_version) IS TRUE
        AND (manifest ->> 'canonicalization' = canonicalization) IS TRUE
        AND (manifest ->> 'mode' = mode) IS TRUE
    ),

    -- Ownership agreement: the manifest subject must describe the same
    -- tenant/principal as the envelope columns. Wrapped in ``IS TRUE`` so a
    -- missing subject.tenant_id / subject.principal_id rejects the row.
    CONSTRAINT chk_context_receipts_subject_tenant CHECK (
        (manifest -> 'subject' ->> 'tenant_id' = tenant_id::text) IS TRUE
    ),
    CONSTRAINT chk_context_receipts_subject_principal CHECK (
        (manifest -> 'subject' ->> 'principal_id' = principal_id::text) IS TRUE
    ),

    -- Packet-hash agreement: manifest.packet.hash must equal the envelope
    -- packet_hash column. Wrapped in ``IS TRUE`` so a missing packet.hash
    -- rejects the row.
    CONSTRAINT chk_context_receipts_packet_agreement CHECK (
        (manifest -> 'packet' ->> 'hash' = packet_hash) IS TRUE
    ),

    -- The manifest must NOT carry a top-level manifest_hash field (it is
    -- computed OVER the manifest and lives outside it). ``?`` returns a
    -- real boolean, so no IS TRUE wrapper is needed here.
    CONSTRAINT chk_context_receipts_no_manifest_hash_field CHECK (
        NOT (manifest ? 'manifest_hash')
    ),

    -- Retention metadata: NULL or not before the receipt's own creation time.
    CONSTRAINT chk_context_receipts_retention CHECK (
        retention_expires_at IS NULL OR retention_expires_at >= created_at
    )
);

-- ============ 2b. CHECK constraint normalization (idempotent) ============
-- A prior revision of this migration created the JSONB CHECK constraints
-- without ``IS TRUE`` wrappers, so an absent JSON key passed as NULL. Re-create
-- those constraints idempotently with the corrected Boolean semantics on
-- databases where the older form is already present.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_receipts'::regclass
          AND conname = 'chk_context_receipts_manifest_sections'
          AND pg_get_constraintdef(oid) NOT LIKE '%IS TRUE%'
    ) THEN
        ALTER TABLE context_receipts
            DROP CONSTRAINT chk_context_receipts_manifest_sections;
        ALTER TABLE context_receipts
            ADD CONSTRAINT chk_context_receipts_manifest_sections CHECK (
                (jsonb_typeof(manifest -> 'subject') = 'object') IS TRUE
                AND (jsonb_typeof(manifest -> 'request') = 'object') IS TRUE
                AND (jsonb_typeof(manifest -> 'versions') = 'object') IS TRUE
                AND (jsonb_typeof(manifest -> 'result') = 'object') IS TRUE
                AND (jsonb_typeof(manifest -> 'packet') = 'object') IS TRUE
                AND (jsonb_typeof(manifest -> 'items') = 'array') IS TRUE
            );
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_receipts'::regclass
          AND conname = 'chk_context_receipts_schema_agreement'
          AND pg_get_constraintdef(oid) NOT LIKE '%IS TRUE%'
    ) THEN
        ALTER TABLE context_receipts
            DROP CONSTRAINT chk_context_receipts_schema_agreement;
        ALTER TABLE context_receipts
            ADD CONSTRAINT chk_context_receipts_schema_agreement CHECK (
                (manifest ->> 'schema' = manifest_schema) IS TRUE
                AND (manifest ->> 'schema_version' = manifest_schema_version) IS TRUE
                AND (manifest ->> 'canonicalization' = canonicalization) IS TRUE
                AND (manifest ->> 'mode' = mode) IS TRUE
            );
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_receipts'::regclass
          AND conname = 'chk_context_receipts_subject_tenant'
          AND pg_get_constraintdef(oid) NOT LIKE '%IS TRUE%'
    ) THEN
        ALTER TABLE context_receipts
            DROP CONSTRAINT chk_context_receipts_subject_tenant;
        ALTER TABLE context_receipts
            ADD CONSTRAINT chk_context_receipts_subject_tenant CHECK (
                (manifest -> 'subject' ->> 'tenant_id' = tenant_id::text) IS TRUE
            );
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_receipts'::regclass
          AND conname = 'chk_context_receipts_subject_principal'
          AND pg_get_constraintdef(oid) NOT LIKE '%IS TRUE%'
    ) THEN
        ALTER TABLE context_receipts
            DROP CONSTRAINT chk_context_receipts_subject_principal;
        ALTER TABLE context_receipts
            ADD CONSTRAINT chk_context_receipts_subject_principal CHECK (
                (manifest -> 'subject' ->> 'principal_id' = principal_id::text) IS TRUE
            );
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'context_receipts'::regclass
          AND conname = 'chk_context_receipts_packet_agreement'
          AND pg_get_constraintdef(oid) NOT LIKE '%IS TRUE%'
    ) THEN
        ALTER TABLE context_receipts
            DROP CONSTRAINT chk_context_receipts_packet_agreement;
        ALTER TABLE context_receipts
            ADD CONSTRAINT chk_context_receipts_packet_agreement CHECK (
                (manifest -> 'packet' ->> 'hash' = packet_hash) IS TRUE
            );
    END IF;
END
$$;

-- ============ 3. One-to-one recall-log relationship ============
-- Composite foreign key (tenant_id, principal_id, recall_log_id) references
-- recall_logs (tenant_id, principal_id, id). This pins all three columns to one
-- parent row so a receipt cannot attach to another tenant's recall log or
-- disagree about the owning principal. ON DELETE RESTRICT (DEFERRABLE INITIALLY
-- DEFERRED): deleting a recall log must not silently erase a retained receipt;
-- future retention logic may intentionally remove the receipt first.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_context_receipts_recall_log'
          AND conrelid = 'context_receipts'::regclass
    ) THEN
        ALTER TABLE context_receipts
            ADD CONSTRAINT fk_context_receipts_recall_log
            FOREIGN KEY (tenant_id, principal_id, recall_log_id)
            REFERENCES recall_logs (tenant_id, principal_id, id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_context_receipts_tenant'
          AND conrelid = 'context_receipts'::regclass
    ) THEN
        ALTER TABLE context_receipts
            ADD CONSTRAINT fk_context_receipts_tenant
            FOREIGN KEY (tenant_id)
            REFERENCES tenants (id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_context_receipts_principal'
          AND conrelid = 'context_receipts'::regclass
    ) THEN
        ALTER TABLE context_receipts
            ADD CONSTRAINT fk_context_receipts_principal
            FOREIGN KEY (tenant_id, principal_id)
            REFERENCES principals (tenant_id, id) ON DELETE RESTRICT;
    END IF;
END
$$;

-- ============ 4. Indexes ============
-- Only indexes with clear future use. ENG-CONTEXT-003 can add query-specific
-- indexes (e.g. a GIN over the manifest) after its API shapes are known.

-- 1. Unique one-to-one: one receipt per recall log.
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_receipts_recall_log
    ON context_receipts (recall_log_id);

-- 2. Principal timeline (newest first).
CREATE INDEX IF NOT EXISTS idx_context_receipts_principal_timeline
    ON context_receipts (tenant_id, principal_id, created_at DESC);

-- 3. Manifest-hash lookup (tenant-scoped).
CREATE INDEX IF NOT EXISTS idx_context_receipts_tenant_manifest_hash
    ON context_receipts (tenant_id, manifest_hash);

-- 4. Retention sweep (only rows with an assigned expiry).
CREATE INDEX IF NOT EXISTS idx_context_receipts_retention_sweep
    ON context_receipts (retention_expires_at)
    WHERE retention_expires_at IS NOT NULL;

-- ============ 5. Row Level Security ============
-- FORCE RLS: the table owner (migration role) is also subject to the policy.
-- The app-role policy requires BOTH tenant and principal — a missing GUC
-- exposes zero rows, a different principal in the same tenant cannot read or
-- insert, and another tenant cannot read or insert. RLS is defense in depth;
-- application code must still use explicit ownership predicates.

ALTER TABLE context_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_receipts FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'context_receipts'
          AND policyname = 'tenant_principal_isolation_context_receipts'
    ) THEN
        DROP POLICY tenant_principal_isolation_context_receipts
            ON context_receipts;
    END IF;
    CREATE POLICY tenant_principal_isolation_context_receipts
        ON context_receipts
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
            AND principal_id::text = current_setting('app.principal_id', true)
        )
        WITH CHECK (
            tenant_id::text = current_setting('app.tenant_id', true)
            AND principal_id::text = current_setting('app.principal_id', true)
        );
END
$$;

-- ============ 6. Append-only grants ============
-- The app role may SELECT and INSERT its own tenant+principal receipts (RLS
-- scopes the rows); UPDATE/DELETE are explicitly revoked so receipts are
-- immutable from the application's perspective. Migration 003's
-- ``ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE`` already
-- granted the app role UPDATE/DELETE on this newly-created table (it is owned
-- by the migration role), so those must be revoked explicitly here.
-- Owner/migration sessions may still manage rows for tests, operations, and a
-- future retention implementation.

GRANT SELECT, INSERT ON context_receipts TO engram_app;
REVOKE UPDATE, DELETE ON context_receipts FROM engram_app;
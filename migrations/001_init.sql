-- Engram — Initial schema migration (v2 — trust model revision)
-- 001_init.sql
--
-- Adds: review states, trust/confidence fields, subject/entity separation,
-- provenance expansion, conflict primitives, item_events audit table,
-- separate embeddings table, full-text search, RLS policies, check constraints.
--
-- Run as: psql -f migrations/001_init.sql

-- ============ Extensions ============

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS vector;       -- requires pgvector 0.8+ for iterative_scan
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- trigram for fuzzy keyword search

-- ============ Identity & Scoping ============

CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, slug)
);

CREATE TABLE principals (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'agent',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, name),
    CHECK (type IN ('agent', 'user', 'system', 'admin'))
);

-- Workspace membership for authorization
CREATE TABLE workspace_members (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    principal_id    UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member',   -- owner | admin | member | viewer
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, principal_id),
    CHECK (role IN ('owner', 'admin', 'member', 'viewer'))
);

-- ============ Memory Items (the core) ============

CREATE TABLE memory_items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    principal_id    UUID NOT NULL REFERENCES principals(id),  -- who wrote this

    -- Content
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,       -- SHA-256 of canonicalized content
    content_tsv     TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(content, ''))
    ) STORED,                             -- full-text search vector (auto-generated)
    kind            TEXT NOT NULL,
    wing            TEXT,
    room            TEXT,

    -- Subject / entity (what is this memory ABOUT — separate from who wrote it)
    subject_type    TEXT,                -- user | agent | repo | project | system | document | task | domain_entity
    subject_id      TEXT,                -- external ID if known (repo name, user handle, etc.)
    subject_name    TEXT,                -- human-readable subject name

    -- Scoping
    visibility      TEXT NOT NULL DEFAULT 'workspace',

    -- Trust & review (the trust loop)
    review_status   TEXT NOT NULL DEFAULT 'proposed',
    -- proposed: written by agent, not yet reviewed
    -- active: reviewed and trusted, enters deterministic recall
    -- disputed: flagged as potentially wrong
    -- rejected: reviewed and rejected (stays for audit, excluded from recall)
    -- archived: old/superseded, excluded from default recall
    memory_confidence   REAL DEFAULT 0.5,    -- overall confidence 0.0-1.0
    source_trust        REAL DEFAULT 0.5,    -- trust in the source (user said it vs agent guessed)
    human_verified      BOOLEAN DEFAULT FALSE,
    verified_by         UUID REFERENCES principals(id),
    verified_at         TIMESTAMPTZ,
    review_notes        TEXT,

    -- Recall ranking
    importance      REAL DEFAULT 0.5,        -- 0.0-1.0, affects recall ordering
    pinned          BOOLEAN DEFAULT FALSE,   -- bypass: included first in startup recall
    last_recalled_at TIMESTAMPTZ,
    recall_count    INTEGER DEFAULT 0,
    last_confirmed_at TIMESTAMPTZ,            -- last time marked useful via /v1/feedback
    startup_recall_count INTEGER DEFAULT 0,  -- times recalled in startup mode (for anti-feedback penalty)
    last_verified_at TIMESTAMPTZ,             -- last time human-verified or conflict-resolved (staleness anchor)

    -- Provenance (expanded)
    source_type     TEXT NOT NULL DEFAULT 'manual',
    source_session  TEXT,
    source_uri      TEXT,                    -- URL, file path, or resource identifier
    extracted_by_model TEXT,                 -- which LLM extracted this (if applicable)
    extraction_confidence REAL,              -- confidence of the extraction (if LLM-derived)

    -- Conflict tracking
    conflicts_with_item_id UUID REFERENCES memory_items(id),
    conflict_type  TEXT,                     -- contradiction | stale | duplicate | scope_overlap
    conflict_resolution_status TEXT,         -- unresolved | accepted | rejected | merged
    conflict_resolved_by UUID REFERENCES principals(id),
    conflict_resolved_at TIMESTAMPTZ,

    -- Privacy / safety
    sensitivity     TEXT NOT NULL DEFAULT 'normal',   -- normal | sensitive | restricted

    -- External linkage (for imports and idempotency)
    external_id     TEXT,                    -- client-provided ID for imports
    external_source TEXT,                    -- source system name (cca, mempalace, github, etc.)

    -- Temporal validity (append-first model)
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,
    superseded_by   UUID REFERENCES memory_items(id),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ============ Check constraints ============
    CONSTRAINT chk_kind CHECK (kind IN (
        'fact', 'preference', 'doctrine', 'decision', 'invariant',
        'observation', 'diary_entry'
    )),
    CONSTRAINT chk_visibility CHECK (visibility IN (
        'private', 'workspace', 'tenant', 'public'
    )),
    CONSTRAINT chk_review_status CHECK (review_status IN (
        'proposed', 'active', 'disputed', 'rejected', 'archived'
    )),
    CONSTRAINT chk_source_type CHECK (source_type IN (
        'manual', 'sync_turn', 'pre_compress', 'session_end',
        'import', 'migration', 'extraction'
    )),
    CONSTRAINT chk_sensitivity CHECK (sensitivity IN (
        'normal', 'sensitive', 'restricted'
    )),
    CONSTRAINT chk_subject_type CHECK (subject_type IS NULL OR subject_type IN (
        'user', 'agent', 'repo', 'project', 'system', 'document', 'task', 'domain_entity'
    )),
    CONSTRAINT chk_confidence_range CHECK (
        memory_confidence >= 0.0 AND memory_confidence <= 1.0
    ),
    CONSTRAINT chk_source_trust_range CHECK (
        source_trust >= 0.0 AND source_trust <= 1.0
    ),
    CONSTRAINT chk_conflict_type CHECK (conflict_type IS NULL OR conflict_type IN (
        'contradiction', 'stale', 'duplicate', 'scope_overlap'
    )),
    CONSTRAINT chk_conflict_resolution CHECK (
        conflict_resolution_status IS NULL OR conflict_resolution_status IN (
            'unresolved', 'accepted', 'rejected', 'merged'
        )
    ),
    -- Composite uniqueness for embedding FK consistency
    UNIQUE(id, tenant_id)
);

-- Full-text search uses a GENERATED column (content_tsv), no trigger needed.
-- The GIN index on content_tsv is created in the indexes section below.

-- ============ Embeddings (separate table — supports multiple models) ============
-- Uses a composite FK to memory_items(id, tenant_id) to enforce that the
-- denormalized tenant_id always matches the parent item's tenant_id.

CREATE TABLE memory_embeddings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    memory_item_id  UUID NOT NULL,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    embedding_model TEXT NOT NULL,           -- e.g. 'text-embedding-3-small'
    embedding_dim   INTEGER NOT NULL,        -- dimension count
    embedding       vector(1536),            -- current default; column type fixed at 1536
    embedded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding_status TEXT NOT NULL DEFAULT 'complete',  -- complete | failed | stale

    -- Composite FK: tenant_id must match the memory_item's tenant_id
    FOREIGN KEY (memory_item_id, tenant_id) REFERENCES memory_items(id, tenant_id) ON DELETE CASCADE,
    UNIQUE(memory_item_id, embedding_model)
);

-- ============ Knowledge Graph ============

CREATE TABLE kg_triples (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    principal_id    UUID REFERENCES principals(id),  -- who wrote this triple
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,
    source_item_id  UUID REFERENCES memory_items(id) ON DELETE SET NULL,
    confidence      REAL DEFAULT 0.5,
    review_status   TEXT NOT NULL DEFAULT 'proposed',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_kg_review CHECK (review_status IN (
        'proposed', 'active', 'disputed', 'rejected', 'archived'
    ))
);

-- ============ Tunnels (cross-domain links) ============

CREATE TABLE tunnels (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_wing     TEXT NOT NULL,
    source_room     TEXT,
    target_wing     TEXT NOT NULL,
    target_room     TEXT,
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Item Events (metadata mutation audit) ============
-- Content is append-first (never updated). Metadata changes (wing, room,
-- visibility, review_status) are audited here so we have a full history
-- of who changed what, when, and why.

CREATE TABLE item_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    item_id         UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,           -- metadata_change | review_change | visibility_change | conflict_flagged | conflict_resolved | verified | pinned | invalidated
    field_name      TEXT,                    -- which field changed
    old_value       TEXT,
    new_value       TEXT,
    actor_principal_id UUID REFERENCES principals(id),
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Classification config ============

CREATE TABLE classification_rules (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    rule_type       TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    target_kind     TEXT,
    target_wing     TEXT,
    target_room     TEXT,
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Tenant trust/recall config (versioned) ============
-- Stores tenant-configurable trust defaults, scoring weights, and recall policy.
-- Versioned for audit reproducibility — recall_logs references config_version.

CREATE TABLE tenant_config (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    config_version              TEXT NOT NULL DEFAULT 'v1',
    -- Scoring weights (must sum to 1.0 for the non-bypass components)
    weight_importance           REAL NOT NULL DEFAULT 0.30,
    weight_source_trust         REAL NOT NULL DEFAULT 0.25,
    weight_memory_confidence    REAL NOT NULL DEFAULT 0.20,
    weight_recency              REAL NOT NULL DEFAULT 0.15,
    weight_verified             REAL NOT NULL DEFAULT 0.10,
    -- Auto-promotion
    auto_promote_enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    auto_promote_confidence_threshold REAL NOT NULL DEFAULT 0.7,
    auto_promote_min_age_hours  INTEGER NOT NULL DEFAULT 72,
    -- Recall limits
    max_pinned_tokens           INTEGER NOT NULL DEFAULT 2048,
    stale_after_days            INTEGER NOT NULL DEFAULT 90,
    startup_recall_penalty_threshold INTEGER NOT NULL DEFAULT 5,
    startup_recall_penalty_factor    REAL NOT NULL DEFAULT 0.5,
    -- Source trust defaults (overridable per tenant)
    trust_manual_user           REAL NOT NULL DEFAULT 0.9,
    trust_manual_agent          REAL NOT NULL DEFAULT 0.6,
    trust_import                REAL NOT NULL DEFAULT 0.8,
    trust_extraction            REAL NOT NULL DEFAULT 0.5,
    trust_sync_turn             REAL NOT NULL DEFAULT 0.4,
    trust_pre_compress          REAL NOT NULL DEFAULT 0.3,
    -- Default memory_confidence per source_type (enables auto-promotion in 1A without LLM)
    confidence_manual_user      REAL NOT NULL DEFAULT 0.9,
    confidence_manual_agent     REAL NOT NULL DEFAULT 0.5,
    confidence_import           REAL NOT NULL DEFAULT 0.8,
    confidence_extraction       REAL NOT NULL DEFAULT 0.5,
    confidence_sync_turn        REAL NOT NULL DEFAULT 0.4,
    confidence_pre_compress     REAL NOT NULL DEFAULT 0.3,
    active                      BOOLEAN NOT NULL DEFAULT TRUE,  -- only one active config per tenant
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_tenant_config_active ON tenant_config(tenant_id) WHERE active = TRUE;

-- ============ Auth ============

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id    UUID REFERENCES principals(id),
    key_hash        TEXT NOT NULL,
    scopes          TEXT[] NOT NULL DEFAULT '{read,write}',
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

-- ============ Recall audit ============

CREATE TABLE recall_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id    UUID NOT NULL REFERENCES principals(id),
    mode            TEXT NOT NULL,
    query           TEXT,
    item_ids        UUID[],
    byte_budget     INTEGER,
    token_budget    INTEGER,
    scoring_version TEXT NOT NULL DEFAULT 'v1',  -- formula version for audit reproducibility
    config_version  TEXT NOT NULL DEFAULT 'v1',  -- tenant config version at recall time
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Deletion audit (tombstone for GDPR/hosted) ============
-- Separate from item_events because physical DELETE breaks FK references.
-- Stores enough to prove deletion occurred without storing deleted content.

CREATE TABLE deletion_events (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    deleted_item_id         UUID NOT NULL,        -- NOT a FK — the item is gone
    deleted_content_hash    TEXT NOT NULL,         -- proves which content was deleted
    deleted_by_principal_id UUID REFERENCES principals(id),
    reason                  TEXT,
    deleted_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Feedback events ============
-- Records per-item feedback from principals. Drives penalty resets,
-- importance adjustment, and the multi-agent quorum rule.

CREATE TABLE feedback_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    item_id         UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    principal_id    UUID NOT NULL REFERENCES principals(id),
    verdict         TEXT NOT NULL,                -- useful | noise
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (verdict IN ('useful', 'noise'))
);

-- ============ Indexes ============

-- Memory items: scoped lookups
CREATE INDEX idx_memitems_tenant_workspace ON memory_items(tenant_id, workspace_id);
CREATE INDEX idx_memitems_taxonomy         ON memory_items(tenant_id, wing, room);
CREATE INDEX idx_memitems_kind             ON memory_items(tenant_id, kind);
CREATE INDEX idx_memitems_active           ON memory_items(tenant_id, valid_to) WHERE valid_to IS NULL;
CREATE INDEX idx_memitems_review           ON memory_items(tenant_id, review_status, valid_to) WHERE valid_to IS NULL;
CREATE INDEX idx_memitems_hash             ON memory_items(tenant_id, workspace_id, principal_id, content_hash) WHERE valid_to IS NULL;
CREATE INDEX idx_memitems_principal        ON memory_items(tenant_id, principal_id);
CREATE INDEX idx_memitems_subject          ON memory_items(tenant_id, subject_type, subject_id);
CREATE INDEX idx_memitems_pinned           ON memory_items(tenant_id, pinned) WHERE pinned = TRUE AND review_status = 'active';
CREATE INDEX idx_memitems_external         ON memory_items(tenant_id, external_source, external_id) WHERE external_id IS NOT NULL;

-- Dedup unique constraint: one active item per (tenant, workspace, principal, content_hash)
-- This makes remember idempotent for retries within scope.
-- NULLS NOT DISTINCT ensures tenant-level memories (workspace_id IS NULL) dedup correctly.
-- Requires PostgreSQL 15+ (pgvector/pgvector:pg16 image satisfies this).
CREATE UNIQUE INDEX idx_memitems_dedup ON memory_items(tenant_id, workspace_id, principal_id, content_hash)
    NULLS NOT DISTINCT
    WHERE valid_to IS NULL AND review_status != 'rejected';

-- Full-text search (GIN index on the tsvector column)
CREATE INDEX idx_memitems_fts ON memory_items USING GIN (content_tsv);

-- Embeddings: semantic search via HNSW
-- Note: pgvector 0.8+ supports iterative_scan for filtered queries.
-- Set at query time: SET hnsw.iterative_scan = strict;
CREATE INDEX idx_embeddings_hnsw ON memory_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Knowledge graph
CREATE INDEX idx_kg_subject     ON kg_triples(tenant_id, subject);
CREATE INDEX idx_kg_predicate   ON kg_triples(tenant_id, predicate);
CREATE INDEX idx_kg_object      ON kg_triples(tenant_id, object);
CREATE INDEX idx_kg_active      ON kg_triples(tenant_id, valid_to) WHERE valid_to IS NULL;
CREATE INDEX idx_kg_source_item ON kg_triples(source_item_id) WHERE source_item_id IS NOT NULL;

-- Tunnels
CREATE INDEX idx_tunnels_wing   ON tunnels(tenant_id, source_wing);

-- Item events
CREATE INDEX idx_events_item    ON item_events(item_id, created_at);

-- API keys
CREATE INDEX idx_apikeys_hash   ON api_keys(key_hash) WHERE revoked_at IS NULL;

-- Classification rules
CREATE INDEX idx_classrules_tenant ON classification_rules(tenant_id, enabled, priority);

-- Feedback events
CREATE INDEX idx_feedback_item    ON feedback_events(tenant_id, item_id, created_at);
CREATE INDEX idx_feedback_principal ON feedback_events(tenant_id, principal_id);

-- Workspace membership
CREATE INDEX idx_wsmembers_ws      ON workspace_members(workspace_id);
CREATE INDEX idx_wsmembers_principal ON workspace_members(principal_id);

-- ============ Views ============

-- Active memory items (reviewed and trusted — enters deterministic recall)
CREATE VIEW active_memories AS
SELECT * FROM memory_items
WHERE review_status = 'active' AND valid_to IS NULL;

-- CCA ledger projection
CREATE VIEW cca_ledger AS
SELECT
    id, tenant_id, workspace_id, principal_id,
    content, kind, wing, room, visibility,
    review_status, memory_confidence, source_trust, human_verified,
    source_type, source_session, source_uri,
    valid_from, valid_to, superseded_by, created_at
FROM memory_items
WHERE kind IN ('doctrine', 'decision', 'invariant', 'preference')
  AND valid_to IS NULL;

-- ============ Row Level Security ============
-- Belt-and-suspenders for multi-tenancy. Even if application code forgets
-- a WHERE clause, Postgres enforces tenant isolation.
-- The application sets the tenant context per connection/session:
--   SET app.tenant_id = '<uuid>';
--   SET app.principal_id = '<uuid>';

ALTER TABLE memory_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE kg_triples ENABLE ROW LEVEL SECURITY;
ALTER TABLE tunnels ENABLE ROW LEVEL SECURITY;
ALTER TABLE item_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE classification_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback_events ENABLE ROW LEVEL SECURITY;

-- NOTE: workspaces and principals are implicitly protected — they are parents
-- referenced by RLS-protected children. Direct cross-tenant reads of these
-- tables are low-risk (they contain only names/slug/type), but for completeness:
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE principals ENABLE ROW LEVEL SECURITY;

-- memory_embeddings has denormalized tenant_id so RLS can apply directly.
-- This enables tenant filtering BEFORE the join to memory_items, which is
-- critical for HNSW query performance with iterative_scan.
ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_embeddings ON memory_embeddings
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_memitems ON memory_items
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_kg ON kg_triples
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_tunnels ON tunnels
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_events ON item_events
    USING (
        EXISTS (
            SELECT 1 FROM memory_items
            WHERE memory_items.id = item_events.item_id
              AND memory_items.tenant_id::text = current_setting('app.tenant_id', true)
        )
    );

CREATE POLICY tenant_isolation_classrules ON classification_rules
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_recalllogs ON recall_logs
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_wsmembers ON workspace_members
    USING (
        EXISTS (
            SELECT 1 FROM workspaces
            WHERE workspaces.id = workspace_members.workspace_id
              AND workspaces.tenant_id::text = current_setting('app.tenant_id', true)
        )
    );

CREATE POLICY tenant_isolation_apikeys ON api_keys
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_tenantconfig ON tenant_config
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_deletion ON deletion_events
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_feedback ON feedback_events
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_workspaces ON workspaces
    USING (tenant_id::text = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_principals ON principals
    USING (tenant_id::text = current_setting('app.tenant_id', true));

-- ============ Seed: default tenant ============

INSERT INTO tenants (name, slug)
VALUES ('Default', 'default')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO workspaces (tenant_id, name, slug)
SELECT id, 'General', 'general' FROM tenants WHERE slug = 'default'
ON CONFLICT (tenant_id, slug) DO NOTHING;

INSERT INTO principals (tenant_id, name, type)
SELECT id, 'admin', 'admin' FROM tenants WHERE slug = 'default'
ON CONFLICT (tenant_id, name) DO NOTHING;

-- Grant workspace membership to admin
INSERT INTO workspace_members (workspace_id, principal_id, role)
SELECT w.id, p.id, 'owner'
FROM tenants t
JOIN workspaces w ON w.tenant_id = t.id AND w.slug = 'general'
JOIN principals p ON p.tenant_id = t.id AND p.name = 'admin'
WHERE t.slug = 'default'
ON CONFLICT (workspace_id, principal_id) DO NOTHING;

-- ============ Seed: default classification rules ============

INSERT INTO classification_rules (tenant_id, name, rule_type, pattern, target_kind, priority)
SELECT t.id, r.name, r.rule_type, r.pattern, r.target_kind, r.priority
FROM tenants t, (VALUES
    ('skip_tool_output',      'regex_skip', '\b(passed|failed|ok|done)\b',                        NULL, 10),
    ('skip_single_token',     'regex_skip', '^.{1,15}$',                                            NULL, 10),
    ('kind_doctrine',         'keyword_kind', 'doctrine|invariant|must|should|always|never',     'doctrine', 50),
    ('kind_decision',         'keyword_kind', 'decided|decision|chose|we will|going to',         'decision', 50),
    ('kind_preference',       'keyword_kind', 'prefers|wants|likes|dislikes|convention',         'preference', 50),
    ('kind_observation',      'keyword_kind', 'observed|noticed|error|failed|warning',           'observation', 60)
) AS r(name, rule_type, pattern, target_kind, priority)
WHERE t.slug = 'default'
ON CONFLICT DO NOTHING;

-- ============ Seed: default tenant config ============

INSERT INTO tenant_config (tenant_id, config_version, active)
SELECT id, 'v1', TRUE FROM tenants WHERE slug = 'default'
ON CONFLICT DO NOTHING;

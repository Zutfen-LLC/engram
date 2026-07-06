-- Engram — Initial schema migration
-- 001_init.sql
--
-- Creates the core tables for shared structured memory:
--   tenants, workspaces, principals, memory_items, kg_triples, tunnels, api_keys, recall_logs
--
-- Run as: psql -f migrations/001_init.sql

-- ============ Extensions ============

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
-- pgvector must be installed in the Postgres image first.
-- The docker-compose.yml uses pgvector/pgvector:pg16 which provides it.
CREATE EXTENSION IF NOT EXISTS vector;

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
    name        TEXT NOT NULL,          -- e.g. "orchestrator", "support-agent"
    type        TEXT NOT NULL DEFAULT 'agent',  -- agent | user
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, name)
);

-- ============ Memory Items (the core) ============

CREATE TABLE memory_items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    principal_id    UUID NOT NULL REFERENCES principals(id),

    -- Content
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,       -- SHA-256 of canonicalized content, for dedup
    kind            TEXT NOT NULL,       -- fact|preference|doctrine|decision|invariant|observation|diary_entry
    wing            TEXT,                -- user-defined taxonomy (optional)
    room            TEXT,

    -- Scoping
    visibility      TEXT NOT NULL DEFAULT 'workspace',  -- private|workspace|tenant|public

    -- Provenance
    source_type     TEXT NOT NULL DEFAULT 'manual',  -- manual|sync_turn|pre_compress|session_end|import|migration
    source_session  TEXT,

    -- Semantic
    embedding       vector(1536),        -- pgvector; dimension configurable in config.py

    -- Temporal validity (append-first model)
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,         -- NULL = currently valid
    superseded_by   UUID REFERENCES memory_items(id),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Knowledge Graph ============

CREATE TABLE kg_triples (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to        TIMESTAMPTZ,
    source_item_id  UUID REFERENCES memory_items(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Tunnels (cross-domain links) ============

CREATE TABLE tunnels (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_wing     TEXT NOT NULL,
    source_room     TEXT NOT NULL,
    target_wing     TEXT NOT NULL,
    target_room     TEXT,
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Auth ============

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id    UUID REFERENCES principals(id),
    key_hash        TEXT NOT NULL,       -- bcrypt/argon2 hash
    scopes          TEXT[] NOT NULL DEFAULT '{read,write}',
    label           TEXT,                -- human-readable name
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

-- ============ Classification config ============

CREATE TABLE classification_rules (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    rule_type       TEXT NOT NULL,       -- keyword_kind | keyword_wing | regex_skip | llm_hint
    pattern         TEXT NOT NULL,       -- keyword string, regex pattern, or hint text
    target_kind     TEXT,                -- if matched, suggest this kind
    target_wing     TEXT,                -- if matched, suggest this wing
    target_room     TEXT,                -- if matched, suggest this room
    priority        INTEGER NOT NULL DEFAULT 100,  -- lower = checked first
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Recall audit ============

CREATE TABLE recall_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id    UUID NOT NULL REFERENCES principals(id),
    mode            TEXT NOT NULL,       -- startup|semantic|keyword
    query           TEXT,
    item_ids        UUID[],
    byte_budget     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ Indexes ============

-- Memory items: scoped lookups
CREATE INDEX idx_memitems_tenant_workspace ON memory_items(tenant_id, workspace_id);
CREATE INDEX idx_memitems_taxonomy         ON memory_items(tenant_id, wing, room);
CREATE INDEX idx_memitems_kind             ON memory_items(tenant_id, kind);
CREATE INDEX idx_memitems_active           ON memory_items(tenant_id, valid_to) WHERE valid_to IS NULL;
CREATE INDEX idx_memitems_hash             ON memory_items(tenant_id, content_hash);
CREATE INDEX idx_memitems_principal        ON memory_items(tenant_id, principal_id);

-- Semantic search (HNSW for high-recall approximate nearest neighbor)
CREATE INDEX idx_memitems_embedding ON memory_items
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Knowledge graph
CREATE INDEX idx_kg_subject     ON kg_triples(tenant_id, subject);
CREATE INDEX idx_kg_predicate   ON kg_triples(tenant_id, predicate);
CREATE INDEX idx_kg_object      ON kg_triples(tenant_id, object);
CREATE INDEX idx_kg_active      ON kg_triples(tenant_id, valid_to) WHERE valid_to IS NULL;

-- Tunnels
CREATE INDEX idx_tunnels_wing   ON tunnels(tenant_id, source_wing);

-- API keys
CREATE INDEX idx_apikeys_hash   ON api_keys(key_hash) WHERE revoked_at IS NULL;

-- Classification rules
CREATE INDEX idx_classrules_tenant ON classification_rules(tenant_id, enabled, priority);

-- ============ Views ============

-- CCA ledger projection: active doctrine/decision/invariant/preference items.
-- Used by /v1/export/cca to generate a git-tracked review artifact.
CREATE VIEW cca_ledger AS
SELECT
    id, tenant_id, workspace_id, principal_id,
    content, kind, wing, room, visibility,
    source_type, source_session,
    valid_from, valid_to, superseded_by, created_at
FROM memory_items
WHERE kind IN ('doctrine', 'decision', 'invariant', 'preference')
  AND valid_to IS NULL;

-- ============ Seed: default tenant (for self-hosted single-tenant use) ============
-- This makes the service usable immediately after migration.
-- The default tenant API key is set via ENGRAM_BOOTSTRAP_KEY env var at runtime.

INSERT INTO tenants (name, slug)
VALUES ('Default', 'default')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO workspaces (tenant_id, name, slug)
SELECT id, 'General', 'general' FROM tenants WHERE slug = 'default'
ON CONFLICT (tenant_id, slug) DO NOTHING;

INSERT INTO principals (tenant_id, name, type)
SELECT id, 'admin', 'user' FROM tenants WHERE slug = 'default'
ON CONFLICT (tenant_id, name) DO NOTHING;

-- ============ Seed: default classification rules (Zutfen-tuned baseline) ============
-- These are the shipped defaults, informed by the zutfen_memory classification work.
-- Tenants can override, disable, or add rules via the /v1/classification/rules API.

INSERT INTO classification_rules (tenant_id, name, rule_type, pattern, target_kind, priority)
SELECT t.id, r.name, r.rule_type, r.pattern, r.target_kind, r.priority
FROM tenants t, (VALUES
    -- Skip patterns: content that should never be remembered
    ('skip_tool_output',      'regex_skip', '\b(passed|failed|ok|done)\b',                        NULL, 10),
    ('skip_single_token',     'regex_skip', '^.{1,15}$',                                            NULL, 10),
    -- Kind inference: keyword → kind
    ('kind_doctrine',         'keyword_kind', 'doctrine|invariant|must|should|always|never',     'doctrine', 50),
    ('kind_decision',         'keyword_kind', 'decided|decision|chose|we will|going to',         'decision', 50),
    ('kind_preference',       'keyword_kind', 'prefers|wants|likes|dislikes|convention',         'preference', 50),
    ('kind_observation',      'keyword_kind', 'observed|noticed|error|failed|warning',           'observation', 60)
) AS r(name, rule_type, pattern, target_kind, priority)
WHERE t.slug = 'default'
ON CONFLICT DO NOTHING;

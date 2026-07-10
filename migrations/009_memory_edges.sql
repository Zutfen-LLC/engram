-- Engram — relationship-aware recall: typed memory-to-memory graph edges
-- (ENG-AUD-012 / F19)
-- 009_memory_edges.sql
--
-- Audit finding F19: recall could find relevant memories but could not
-- reconstruct the context surrounding them (related decisions, explanations,
-- contradictions). This migration adds ``memory_edges`` — a bounded, typed,
-- directed relationship between two memory_items rows — which
-- engram.relationship_recall uses for depth-1 graph expansion during semantic
-- recall. It is distinct from ``kg_triples`` (free-text subject/predicate/
-- object facts); an edge always links two concrete memory items.
--
-- Run as: psql -f migrations/009_memory_edges.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

CREATE TABLE IF NOT EXISTS memory_edges (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_item_id  UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    target_item_id  UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    edge_type       TEXT NOT NULL,
    weight          FLOAT,
    principal_id    UUID REFERENCES principals(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT memory_edges_no_self_loop CHECK (source_item_id <> target_item_id),
    CONSTRAINT memory_edges_type_check CHECK (
        edge_type IN (
            'derived_from', 'references', 'explains',
            'contradicts', 'supports', 'depends_on', 'mentions'
        )
    ),
    CONSTRAINT memory_edges_unique UNIQUE (tenant_id, source_item_id, target_item_id, edge_type)
);

-- Neighbor lookups run in both directions (see
-- engram.relationship_recall._fetch_graph_neighbors), so both endpoints need
-- an index.
CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_edges (tenant_id, source_item_id);
CREATE INDEX IF NOT EXISTS idx_memory_edges_target ON memory_edges (tenant_id, target_item_id);

ALTER TABLE memory_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_edges FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'memory_edges'
          AND policyname = 'tenant_isolation_memory_edges'
    ) THEN
        CREATE POLICY tenant_isolation_memory_edges ON memory_edges
            USING (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

-- engram_app already receives SELECT/INSERT/UPDATE/DELETE on this table via
-- the ALTER DEFAULT PRIVILEGES grant from migrations/003_app_role_and_force_rls.sql
-- (new tables created by the owner role are auto-granted), so no explicit
-- GRANT is needed here.

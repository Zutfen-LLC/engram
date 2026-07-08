-- Supporting indexes for embedding backfill (BL-006).
--
-- The backfill streams candidates one batch at a time with keyset pagination and
-- counts work/skips via count(*) queries, all filtering
-- ``memory_embeddings`` by ``(tenant_id, embedding_model)`` and ordering by
-- ``embedded_at``. Without a tenant-leading btree these are full table scans +
-- sorts (once per streamed page), so a large backlog degrades badly. The same
-- index serves the missing-row anti-join subquery.
--
-- ``memory_items`` is ordered by ``(created_at, id)`` for the missing-row stream;
-- the existing ``idx_memitems_active`` filters tenant+active but cannot serve
-- that ordering, so a partial index over live rows is added.
--
-- IF NOT EXISTS keeps the file safe to re-apply on existing databases
-- (the init scripts run once via docker-entrypoint-initdb.d; operators apply
-- later migrations manually with ``psql -f migrations/002_backfill_indexes.sql``).

CREATE INDEX IF NOT EXISTS idx_memembed_backfill
    ON memory_embeddings (tenant_id, embedding_model, embedded_at, id);

CREATE INDEX IF NOT EXISTS idx_memitems_backfill
    ON memory_items (tenant_id, created_at, id)
    WHERE valid_to IS NULL;

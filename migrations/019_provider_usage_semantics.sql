-- ENG-METER-003: durable provider-operation semantics and reporting indexes.
-- Safe to re-apply: columns, constraints, and indexes are guarded/idempotent.

ALTER TABLE usage_events
    ADD COLUMN IF NOT EXISTS usage_class TEXT NULL,
    ADD COLUMN IF NOT EXISTS external_call_attempted BOOLEAN NULL;

UPDATE usage_events
   SET external_call_attempted = CASE WHEN status = 'disabled' THEN false ELSE true END,
       usage_class = CASE
           WHEN operation IN ('embedding_backfill') THEN 'maintenance'
           WHEN operation IN ('embedding_setup') THEN 'diagnostic'
           WHEN operation IN ('embedding_query_recall', 'embedding_query_search') THEN 'request'
           WHEN operation IN ('embedding_document') THEN 'async_enrichment'
           ELSE 'unknown'
       END
 WHERE event_type = 'provider.call'
   AND (external_call_attempted IS NULL OR usage_class IS NULL);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_usage_events_provider_semantics'
    ) THEN
        ALTER TABLE usage_events ADD CONSTRAINT chk_usage_events_provider_semantics CHECK (
            event_type <> 'provider.call' OR (
                usage_class IN (
                    'request', 'async_enrichment', 'maintenance', 'diagnostic', 'unknown'
                )
                AND external_call_attempted IS NOT NULL
                AND (status <> 'disabled' OR external_call_attempted = false)
                AND (status <> 'succeeded' OR external_call_attempted = true)
            )
        );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_usage_events_candidate_outcome_resolution
    ON usage_events (tenant_id, correlation_id, created_at, id)
    WHERE event_type = 'candidate.outcome' AND correlation_id IS NOT NULL;

-- Supports report grouping/filtering by product-vs-maintenance inference class.
CREATE INDEX IF NOT EXISTS idx_usage_events_provider_class_created
    ON usage_events (tenant_id, usage_class, created_at)
    WHERE event_type = 'provider.call';

GRANT SELECT, INSERT ON usage_events TO engram_app;
REVOKE UPDATE, DELETE ON usage_events FROM engram_app;

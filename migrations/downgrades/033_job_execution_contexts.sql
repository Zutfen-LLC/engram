-- Deterministic rollback for migration 033.
-- job_execution_contexts rows are the only durable execution authority for
-- queued jobs whose payload references them (promotion-evaluate-v2). Refuse to
-- discard rows while any job still depends on one, so a downgrade can never
-- silently strip authority from live queue work.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM job_execution_contexts jec
        JOIN jobs j
          ON j.payload->>'execution_context_id' = jec.id::text
    ) THEN
        RAISE EXCEPTION
            'migration 033 downgrade requires no jobs referencing job_execution_contexts'
            USING ERRCODE = '55000';
    END IF;
END
$$;

DROP TABLE IF EXISTS job_execution_contexts;

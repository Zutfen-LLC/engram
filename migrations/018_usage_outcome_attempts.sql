-- Engram — Append-only candidate.outcome attempts (ENG-METER-001 correction)
-- 018_usage_outcome_attempts.sql
--
-- candidate.outcome is now append-only PER ATTEMPT: every /v1/remember
-- invocation appends its own row (no dedupe_key), so a transiently failed
-- attempt followed by a successful retry is recorded honestly as two rows.
-- candidate.observed remains unique per correlation_id (one observation per
-- candidate). The report derives a single LOGICAL outcome per correlation_id
-- (earliest non-'failed' attempt, else 'failed') for the failure/create
-- funnel, and reports attempt-level diagnostics separately.
--
-- This migration clears any pre-existing candidate.outcome dedupe_keys so the
-- new contract is uniform across old and new rows (older rows written under
-- the first-outcome-wins model had dedupe_key = str(correlation_id); clearing
-- them makes the partial unique index idx_usage_events_dedupe no longer
-- constrain candidate.outcome inserts). It is a no-op on fresh databases.
--
-- Run as: psql -f migrations/018_usage_outcome_attempts.sql  (owner/migration role)
-- Safe to re-apply: the UPDATE is idempotent.

UPDATE usage_events
   SET dedupe_key = NULL
 WHERE event_type = 'candidate.outcome'
   AND dedupe_key IS NOT NULL;

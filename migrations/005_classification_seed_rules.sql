-- Engram — classification seed-rule correctness (ENG-AUD-005)
-- 005_classification_seed_rules.sql
--
-- Audit finding F9: the default seed classification rules misclassify common
-- agent output.
--
--   * ``skip_tool_output`` = \b(passed|failed|ok|done)\b matched status words
--     *inside* meaningful sentences ("the deploy is done", "tests passed after
--     the fix") and forced a conservative fact default — silently suppressing
--     the kind/wing/room rules for exactly the content they were written for.
--   * ``skip_single_token`` = ^.{1,15}$ matched any short text (including short
--     sentences), not just bare status tokens.
--   * ``kind_doctrine`` = doctrine|invariant|must|should|always|never promoted
--     casual statements to the highest-stakes kind on any modal verb.
--
-- This migration reworks the seeds for ALL tenants:
--   * rename ``skip_tool_output`` → ``skip_status_only`` and anchor it with
--     \A...\Z so it only matches whole-message status text;
--   * tighten ``skip_single_token`` to a whole-string single short token;
--   * make ``kind_doctrine`` require explicit policy/invariant phrasing or
--     ``must (never|always|not)`` rather than bare modal verbs.
--
-- These mirror the updated seeds in 001_init.sql so fresh installs and upgrades
-- converge on the same rule set.
--
-- Run as: psql -f migrations/005_classification_seed_rules.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent.

-- Remove the old over-broad skip rule by name (it was only ever seeded, so the
-- name uniquely identifies it). ``ON CONFLICT DO NOTHING`` on the new name below
-- means re-running this migration is a no-op once the new rule exists.
DELETE FROM classification_rules
WHERE name = 'skip_tool_output';

-- Tighten single-token skip: whole-string single short token only.
UPDATE classification_rules
SET pattern = '\A\S{1,12}\Z'
WHERE name = 'skip_single_token';

-- Doctrine requires explicit labels or "must (never|always|not)"; bare
-- should/always/never/must no longer promote to doctrine.
UPDATE classification_rules
SET pattern = '\b(doctrine|invariant)\b|policy\s*:|rule\s*:|invariant\s*:|must\s+(never|always|not)'
WHERE name = 'kind_doctrine';

-- Insert the new status-only skip rule for every tenant that does not already
-- have it. (Existing tenants had skip_tool_output, now deleted above.)
INSERT INTO classification_rules (tenant_id, name, rule_type, pattern, target_kind, priority)
SELECT t.id, 'skip_status_only', 'regex_skip',
       '\A\s*(ok|done|passed|failed|success(ful)?|all\s+good|ack|acknowledged|got\s+it|will\s+do)\s*[.!?]*\s*\Z',
       NULL, 10
FROM tenants t
WHERE NOT EXISTS (
    SELECT 1 FROM classification_rules cr
    WHERE cr.tenant_id = t.id AND cr.name = 'skip_status_only'
);

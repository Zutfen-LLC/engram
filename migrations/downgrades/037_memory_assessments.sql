-- Rollback preserves immutable history. Disable assessment flags before this step.
-- Tables remain queryable for audit. Reapplication of migration 037 is idempotent.
SELECT 1;

-- This is a forward-only schema widening. The downgrade removes only the
-- additive recall-log field. It retains the widened receipt constraints and
-- all semantic receipt rows because narrowing could invalidate history.
ALTER TABLE recall_logs DROP COLUMN IF EXISTS item_budget;

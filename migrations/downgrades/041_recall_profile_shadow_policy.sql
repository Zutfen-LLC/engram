-- Downgrade for 041_recall_profile_shadow_policy.sql.
--
-- Dropping the policy column removes only the tenant's allow decision for
-- the shadow comparison surface; the surface itself fails closed when the
-- column is absent, so no served behavior changes.

ALTER TABLE tenant_config
    DROP COLUMN IF EXISTS recall_profile_shadow_enabled;

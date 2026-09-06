-- Tenant policy for candidate recall-profile inspection (issue #160
-- correction: exploratory/governed are shadow-only until #162 certification).
--
-- Additive, fails closed: BOOLEAN NOT NULL DEFAULT FALSE. The shadow
-- comparison surface (POST /v1/recall/shadow-compare) requires REVIEW_SCOPE
-- AND this tenant allow before evaluating governed/exploratory candidate
-- packets; an absent or false policy denies inspection even for a capable
-- caller. This column never affects what POST /v1/recall serves — ordinary
-- recall keeps serving the legacy packet until a code-level certification
-- constant changes (recall_profiles.CERTIFIED_SERVING_PROFILES).

ALTER TABLE tenant_config
    ADD COLUMN IF NOT EXISTS recall_profile_shadow_enabled BOOLEAN NOT NULL DEFAULT FALSE;

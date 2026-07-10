-- ENG-AUD-009: profile-keyed, variable-dimension embeddings.
-- Safe on fresh installs and populated databases; existing vectors are preserved.

CREATE TABLE IF NOT EXISTS embedding_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    distance_metric TEXT NOT NULL DEFAULT 'cosine'
        CHECK (distance_metric IN ('cosine')),
    state TEXT NOT NULL DEFAULT 'candidate'
        CHECK (state IN ('candidate', 'active', 'retired')),
    index_status TEXT NOT NULL DEFAULT 'missing'
        CHECK (index_status IN ('missing', 'creating', 'ready', 'failed')),
    index_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_embedding_profiles_one_active
    ON embedding_profiles ((state)) WHERE state = 'active';

-- Central legacy identity. Existing rows are discovered by model/dimension;
-- the empty-database seed matches the application defaults.
INSERT INTO embedding_profiles
    (profile_key, provider, model, dimensions, state, index_status, index_name,
     activated_at, metadata)
SELECT DISTINCT
    'openai:' || embedding_model || ':' || embedding_dim,
    'openai', embedding_model, embedding_dim,
    CASE WHEN embedding_model = 'text-embedding-3-small' AND embedding_dim = 1536
         THEN 'active' ELSE 'retired' END,
    CASE WHEN embedding_model = 'text-embedding-3-small' AND embedding_dim = 1536
         THEN 'ready' ELSE 'missing' END,
    CASE WHEN embedding_model = 'text-embedding-3-small' AND embedding_dim = 1536
         THEN 'idx_embeddings_profile_legacy' ELSE NULL END,
    CASE WHEN embedding_model = 'text-embedding-3-small' AND embedding_dim = 1536
         THEN now() ELSE NULL END,
    '{"legacy": true}'::jsonb
FROM memory_embeddings
ON CONFLICT (profile_key) DO NOTHING;

INSERT INTO embedding_profiles
    (profile_key, provider, model, dimensions, state, index_status, index_name,
     activated_at, metadata)
SELECT 'openai:text-embedding-3-small:1536', 'openai',
       'text-embedding-3-small', 1536, 'active', 'ready',
       'idx_embeddings_profile_legacy', now(), '{"legacy": true}'::jsonb
WHERE NOT EXISTS (SELECT 1 FROM embedding_profiles WHERE state = 'active')
ON CONFLICT (profile_key) DO UPDATE SET
    state = 'active', index_status = 'ready',
    index_name = 'idx_embeddings_profile_legacy', activated_at = now();

ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS profile_id UUID;

UPDATE memory_embeddings me
SET profile_id = ep.id
FROM embedding_profiles ep
WHERE me.profile_id IS NULL
  AND ep.model = me.embedding_model
  AND ep.dimensions = me.embedding_dim;

-- Historical DDL used status='complete'; populated rows are ready in the live
-- vocabulary. This is the only status normalization and does not alter vectors.
UPDATE memory_embeddings SET embedding_status = 'ready'
WHERE embedding IS NOT NULL AND embedding_status = 'complete';

CREATE OR REPLACE FUNCTION active_embedding_profile_id() RETURNS UUID
LANGUAGE sql STABLE AS $$
    SELECT id FROM embedding_profiles WHERE state = 'active' LIMIT 1
$$;

ALTER TABLE memory_embeddings
    DROP CONSTRAINT IF EXISTS memory_embeddings_memory_item_id_embedding_model_key;
DROP INDEX IF EXISTS idx_embeddings_hnsw;

ALTER TABLE memory_embeddings
    ALTER COLUMN embedding TYPE vector USING embedding::vector;

ALTER TABLE memory_embeddings
    ALTER COLUMN profile_id SET NOT NULL;
ALTER TABLE memory_embeddings
    ALTER COLUMN profile_id SET DEFAULT active_embedding_profile_id();

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_memory_embeddings_profile') THEN
        ALTER TABLE memory_embeddings ADD CONSTRAINT fk_memory_embeddings_profile
            FOREIGN KEY (profile_id) REFERENCES embedding_profiles(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_memory_embeddings_item_profile') THEN
        ALTER TABLE memory_embeddings ADD CONSTRAINT uq_memory_embeddings_item_profile
            UNIQUE (tenant_id, memory_item_id, profile_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_memory_embedding_dims') THEN
        ALTER TABLE memory_embeddings ADD CONSTRAINT chk_memory_embedding_dims
            CHECK (embedding IS NULL OR vector_dims(embedding) = embedding_dim);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_embeddings_profile_lookup
    ON memory_embeddings (tenant_id, profile_id, embedding_status, embedded_at, id);

-- Preserve an immediately usable index for the legacy active profile.
DO $$ DECLARE legacy_id UUID;
BEGIN
    SELECT id INTO legacy_id FROM embedding_profiles
    WHERE profile_key = 'openai:text-embedding-3-small:1536';
    IF legacy_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_embeddings_profile_legacy'
    ) THEN
        EXECUTE format(
            'CREATE INDEX idx_embeddings_profile_legacy ON memory_embeddings '
            'USING hnsw ((embedding::vector(1536)) vector_cosine_ops) '
            'WITH (m = 16, ef_construction = 64) '
            'WHERE profile_id = %L::uuid AND embedding_dim = 1536 '
            'AND embedding_status = ''ready''', legacy_id
        );
    END IF;
END $$;

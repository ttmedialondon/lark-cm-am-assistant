-- One credential per AI function.  The key itself is always encrypted by the
-- application before this table is written.

CREATE TABLE ai_feature_configs (
    feature TEXT PRIMARY KEY CHECK (length(btrim(feature)) BETWEEN 1 AND 80),
    provider TEXT NOT NULL CHECK (length(btrim(provider)) BETWEEN 1 AND 40),
    project_id TEXT NOT NULL CHECK (length(btrim(project_id)) BETWEEN 1 AND 160),
    model TEXT NOT NULL CHECK (length(btrim(model)) BETWEEN 1 AND 160),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    api_key_ciphertext TEXT,
    key_suffix TEXT CHECK (key_suffix IS NULL OR length(key_suffix) = 4),
    key_updated_at TIMESTAMPTZ,
    key_updated_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    last_tested_at TIMESTAMPTZ,
    last_test_status TEXT NOT NULL DEFAULT 'never'
        CHECK (last_test_status IN ('never', 'succeeded', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (api_key_ciphertext IS NULL AND key_suffix IS NULL)
        OR (api_key_ciphertext IS NOT NULL AND key_suffix IS NOT NULL)
    ),
    CHECK (api_key_ciphertext IS NOT NULL OR key_updated_at IS NULL),
    CHECK (NOT enabled OR api_key_ciphertext IS NOT NULL)
);

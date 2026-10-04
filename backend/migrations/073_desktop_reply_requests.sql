CREATE TABLE IF NOT EXISTS desktop_reply_requests (
    actor_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    request_id UUID NOT NULL,
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'abandoned')),
    fence_token UUID NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (clock_timestamp() + INTERVAL '24 hours'),
    result_ciphertext TEXT,
    error_code TEXT CHECK (char_length(error_code) <= 64),
    error_message TEXT CHECK (char_length(error_message) <= 300),
    error_http_status INTEGER CHECK (error_http_status BETWEEN 400 AND 599),
    usage JSONB NOT NULL DEFAULT '{}',
    latency_ms INTEGER CHECK (latency_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (actor_id, request_id),
    CHECK ((status = 'succeeded') = (result_ciphertext IS NOT NULL)),
    CHECK ((status IN ('failed', 'cancelled', 'abandoned')) = (error_code IS NOT NULL)),
    CHECK ((error_code IS NULL) = (error_message IS NULL)),
    CHECK ((error_code IS NULL) = (error_http_status IS NULL))
);

CREATE INDEX IF NOT EXISTS desktop_reply_requests_actor_expiry_idx
    ON desktop_reply_requests (actor_id, expires_at);

-- Reuse the configured provider credential without enabling any background workflow.
-- The desktop feature has its own switch and no shared AI admission/queue entry.
INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,
                              key_suffix,key_updated_at,key_updated_by_authorization_id)
SELECT 'desktop_reply',provider,project_id,model,FALSE,api_key_ciphertext,
       key_suffix,key_updated_at,key_updated_by_authorization_id
FROM ai_feature_configs WHERE feature='offhours_summary'
ON CONFLICT(feature) DO NOTHING;

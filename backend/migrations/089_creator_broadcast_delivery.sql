-- Frozen, confirmed sends. Plain message bodies are never stored in these tables.
ALTER TABLE creator_broadcast_lists DROP CONSTRAINT creator_broadcast_lists_purpose_check;
ALTER TABLE creator_broadcast_lists ADD CONSTRAINT creator_broadcast_lists_purpose_check
    CHECK (purpose IN ('support','community','training','category','campaign','custom'));

CREATE TABLE creator_broadcast_previews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    sender_open_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('support','community','training','category','campaign','custom')),
    body_ciphertext TEXT NOT NULL,
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '15 minutes'
);

CREATE TABLE creator_broadcast_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    preview_id UUID NOT NULL UNIQUE REFERENCES creator_broadcast_previews(id) ON DELETE RESTRICT,
    request_id UUID NOT NULL,
    cancel_requested_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sender_authorization_id,request_id)
);
CREATE INDEX creator_broadcast_batches_sender
    ON creator_broadcast_batches(sender_authorization_id,created_at DESC,id);
CREATE INDEX creator_broadcast_previews_sender_fingerprint
    ON creator_broadcast_previews(sender_authorization_id,fingerprint,created_at DESC);

CREATE TABLE creator_broadcast_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES creator_broadcast_batches(id) ON DELETE RESTRICT,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL,
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    message_uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','sending','succeeded','failed','unknown','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    send_started_at TIMESTAMPTZ,
    message_id TEXT,
    error_message TEXT,
    http_status INTEGER,
    lark_code INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id,chat_id),
    UNIQUE (message_uuid),
    CHECK (status<>'succeeded' OR message_id IS NOT NULL),
    CHECK (NOT retryable OR status='failed')
);
CREATE INDEX creator_broadcast_targets_claim
    ON creator_broadcast_targets(next_attempt_at,created_at,id) WHERE status='pending';
CREATE INDEX creator_broadcast_targets_batch ON creator_broadcast_targets(batch_id,created_at,id);
CREATE INDEX creator_broadcast_targets_lease ON creator_broadcast_targets(lease_until) WHERE status='sending';

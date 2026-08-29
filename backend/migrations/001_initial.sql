CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    code_verifier_ciphertext TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS oauth_states_expires_at_idx ON oauth_states (expires_at);

CREATE TABLE IF NOT EXISTS authorizations (
    id UUID PRIMARY KEY,
    open_id TEXT NOT NULL UNIQUE,
    union_id TEXT,
    display_name TEXT,
    avatar_url TEXT,
    access_token_ciphertext TEXT NOT NULL,
    refresh_token_ciphertext TEXT,
    access_token_expires_at TIMESTAMPTZ,
    refresh_token_expires_at TIMESTAMPTZ,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'attention_required', 'revoked')),
    last_error TEXT,
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS web_sessions (
    session_hash TEXT PRIMARY KEY,
    authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS web_sessions_authorization_idx ON web_sessions (authorization_id);
CREATE INDEX IF NOT EXISTS web_sessions_expires_at_idx ON web_sessions (expires_at);

CREATE TABLE IF NOT EXISTS user_chats (
    authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    name TEXT,
    external BOOLEAN NOT NULL DEFAULT FALSE,
    chat_status TEXT,
    chat_mode TEXT,
    sync_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    is_visible BOOLEAN NOT NULL DEFAULT TRUE,
    sync_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (sync_status IN ('pending', 'syncing', 'succeeded', 'not_readable', 'failed_retryable')),
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    backfill_page_token TEXT,
    backfill_complete BOOLEAN NOT NULL DEFAULT FALSE,
    backfill_started_at TIMESTAMPTZ,
    backfill_window_start_at TIMESTAMPTZ,
    backfill_window_end_at TIMESTAMPTZ,
    last_backfill_at TIMESTAMPTZ,
    incremental_page_token TEXT,
    incremental_job_id UUID,
    incremental_window_start_at TIMESTAMPTZ,
    incremental_window_end_at TIMESTAMPTZ,
    last_incremental_at TIMESTAMPTZ,
    last_error TEXT,
    PRIMARY KEY (authorization_id, chat_id)
);

CREATE INDEX IF NOT EXISTS user_chats_sync_idx
    ON user_chats (authorization_id, sync_enabled, is_visible, backfill_complete);

CREATE TABLE IF NOT EXISTS message_ledger (
    authorization_id UUID NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_create_time TIMESTAMPTZ,
    sent_by_author BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (authorization_id, chat_id, message_id),
    FOREIGN KEY (authorization_id, chat_id)
        REFERENCES user_chats (authorization_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS message_ledger_stats_idx
    ON message_ledger (authorization_id, sent_by_author);
CREATE INDEX IF NOT EXISTS message_ledger_chat_time_idx
    ON message_ledger (authorization_id, chat_id, message_create_time);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id UUID PRIMARY KEY,
    authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('backfill', 'incremental')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'attention_required', 'cancelled')),
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    pages_processed INTEGER NOT NULL DEFAULT 0,
    chats_discovered INTEGER NOT NULL DEFAULT 0,
    messages_indexed INTEGER NOT NULL DEFAULT 0,
    own_messages_indexed INTEGER NOT NULL DEFAULT 0,
    last_error_code INTEGER,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS sync_jobs_claim_idx ON sync_jobs (status, scheduled_at);
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_one_active_per_authorization_idx
    ON sync_jobs (authorization_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY,
    authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS audit_events_authorization_idx ON audit_events (authorization_id, created_at DESC);

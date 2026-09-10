-- An independent, disabled-by-default pilot. No old identity AI is enabled.
CREATE TABLE offhours_summary_runs (
    id UUID PRIMARY KEY,
    requested_by_authorization_id UUID NOT NULL REFERENCES authorizations(id),
    responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id),
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL CHECK (window_end > window_start),
    timezone TEXT NOT NULL DEFAULT 'Europe/London',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    coverage JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE offhours_summary_settings (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    run_id UUID NOT NULL REFERENCES offhours_summary_runs(id),
    config_fingerprint TEXT NOT NULL,
    next_check_at TIMESTAMPTZ,
    refresh_until TIMESTAMPTZ NOT NULL
);

CREATE TABLE offhours_summary_tasks (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES offhours_summary_runs(id),
    chat_id TEXT NOT NULL REFERENCES chats(chat_id),
    input_fingerprint TEXT NOT NULL,
    cutoff TIMESTAMPTZ NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','succeeded','failed','invalid','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    result_ciphertext TEXT,
    error_message TEXT,
    model TEXT,
    response_id TEXT,
    usage JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    UNIQUE (run_id, chat_id, input_fingerprint)
);
CREATE INDEX offhours_summary_tasks_claim_idx ON offhours_summary_tasks(scheduled_at, created_at)
    WHERE status = 'queued';
CREATE INDEX offhours_summary_tasks_latest_idx ON offhours_summary_tasks(run_id, chat_id, created_at DESC);
CREATE UNIQUE INDEX offhours_summary_tasks_current_idx ON offhours_summary_tasks(run_id,chat_id) WHERE is_current;

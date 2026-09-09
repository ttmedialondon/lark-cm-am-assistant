-- Independent, explicitly enabled candidate-only queue. Never enable the old
-- contact-role or contact-assignment features, or migrate their derived facts.
CREATE TABLE creator_workflow_ai_runs (
    id UUID PRIMARY KEY,
    responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id),
    requested_by_authorization_id UUID NOT NULL REFERENCES authorizations(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    history_days INTEGER NOT NULL CHECK (history_days BETWEEN 1 AND 90),
    chat_limit INTEGER NOT NULL CHECK (chat_limit BETWEEN 1 AND 100),
    continuous BOOLEAN NOT NULL DEFAULT FALSE,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    total_chats INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    next_scan_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '15 minutes'
);
CREATE UNIQUE INDEX creator_workflow_ai_runs_active_cm_idx
    ON creator_workflow_ai_runs(responsible_authorization_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE creator_workflow_ai_tasks (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES creator_workflow_ai_runs(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL REFERENCES chats(chat_id),
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'invalid', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    model TEXT,
    response_id TEXT,
    usage JSONB NOT NULL DEFAULT '{}',
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    UNIQUE (run_id, chat_id)
);
CREATE INDEX creator_workflow_ai_tasks_claim_idx ON creator_workflow_ai_tasks(scheduled_at, created_at)
    WHERE status = 'queued';
CREATE INDEX creator_workflow_ai_tasks_fingerprint_idx
    ON creator_workflow_ai_tasks(chat_id, input_fingerprint, status);

-- Persist metadata inspection progress too: an unchanged/empty first batch must
-- not starve the rest of a large opted-in CM scope on subsequent bounded scans.
CREATE TABLE creator_workflow_ai_chat_checks (
    responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id),
    chat_id TEXT NOT NULL REFERENCES chats(chat_id),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (responsible_authorization_id, chat_id)
);

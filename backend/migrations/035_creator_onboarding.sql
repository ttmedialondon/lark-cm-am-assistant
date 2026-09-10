-- Register only from a genuinely new OAuth authorization, never seed old users.
CREATE TABLE creator_workflow_onboarding (
    authorization_id UUID PRIMARY KEY REFERENCES authorizations(id),
    status TEXT NOT NULL DEFAULT 'waiting_sync'
        CHECK (status IN ('waiting_sync','scanning','succeeded','failed','cancelled')),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ,
    consent_at TIMESTAMPTZ,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    last_chat_id TEXT,
    run_id UUID REFERENCES creator_workflow_ai_runs(id) ON DELETE SET NULL,
    total_chats INTEGER NOT NULL DEFAULT 0,
    checked_chats INTEGER NOT NULL DEFAULT 0,
    queued_chats INTEGER NOT NULL DEFAULT 0,
    truncated_chats INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
ALTER TABLE creator_workflow_ai_runs
    ADD COLUMN onboarding_authorization_id UUID REFERENCES creator_workflow_onboarding(authorization_id);
CREATE INDEX creator_workflow_onboarding_due_idx ON creator_workflow_onboarding(next_check_at)
    WHERE status IN ('waiting_sync','scanning');

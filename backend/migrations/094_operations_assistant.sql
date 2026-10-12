CREATE TABLE assistant_sessions (
    id UUID PRIMARY KEY,
    actor_id UUID NOT NULL REFERENCES authorizations(id),
    context_ciphertext TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX assistant_sessions_actor_idx ON assistant_sessions(actor_id, updated_at DESC);

CREATE TABLE assistant_turns (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES assistant_sessions(id),
    client_request_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','waiting','completed','cancelled','failed','unknown')),
    input_ciphertext TEXT NOT NULL,
    checkpoint_ciphertext TEXT,
    result_ciphertext TEXT,
    usage JSONB NOT NULL DEFAULT '{}',
    response_id TEXT,
    error_code TEXT,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    UNIQUE(session_id, client_request_id)
);
CREATE UNIQUE INDEX assistant_turn_active_idx ON assistant_turns(session_id)
    WHERE status IN ('queued','running','waiting');
CREATE INDEX assistant_turn_claim_idx ON assistant_turns(scheduled_at,created_at) WHERE status='queued';

ALTER TABLE offhours_summary_runs
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'scheduled' CHECK(origin IN ('scheduled','assistant')),
    ADD COLUMN assistant_turn_id UUID REFERENCES assistant_turns(id),
    ADD COLUMN selected_contact_ids UUID[],
    ADD CONSTRAINT assistant_run_origin CHECK (
        (origin='scheduled' AND assistant_turn_id IS NULL) OR
        (origin='assistant' AND assistant_turn_id IS NOT NULL AND period IS NULL)
    );
CREATE INDEX assistant_run_turn_idx ON offhours_summary_runs(assistant_turn_id) WHERE origin='assistant';
ALTER TABLE summary_reply_tasks ADD COLUMN feature TEXT NOT NULL DEFAULT 'offhours_summary'
    CHECK(feature IN ('offhours_summary','operations_assistant'));

INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled)
    VALUES('operations_assistant','openai','unconfigured','gpt-6-luna',FALSE)
    ON CONFLICT(feature) DO NOTHING;

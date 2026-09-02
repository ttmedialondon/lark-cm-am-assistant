-- Both current AI features use the same OpenAI Project.  A single persisted
-- gate keeps their combined in-flight requests bounded even after a worker
-- restart; PostgreSQL advisory locking provides the atomic admission check.
CREATE TABLE ai_admission_state (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    not_before TIMESTAMPTZ,
    paused_at TIMESTAMPTZ,
    pause_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO ai_admission_state (id) VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE contact_assessments
    ADD COLUMN openai_request_id TEXT,
    ADD COLUMN response_latency_ms INTEGER CHECK (response_latency_ms IS NULL OR response_latency_ms >= 0),
    ADD COLUMN rate_limit_remaining_requests INTEGER
        CHECK (rate_limit_remaining_requests IS NULL OR rate_limit_remaining_requests >= 0),
    ADD COLUMN rate_limit_remaining_tokens INTEGER
        CHECK (rate_limit_remaining_tokens IS NULL OR rate_limit_remaining_tokens >= 0),
    ADD COLUMN rate_limit_reset_requests TEXT,
    ADD COLUMN rate_limit_reset_tokens TEXT;

ALTER TABLE contact_assignment_assessments
    ADD COLUMN openai_request_id TEXT,
    ADD COLUMN response_latency_ms INTEGER CHECK (response_latency_ms IS NULL OR response_latency_ms >= 0),
    ADD COLUMN rate_limit_remaining_requests INTEGER
        CHECK (rate_limit_remaining_requests IS NULL OR rate_limit_remaining_requests >= 0),
    ADD COLUMN rate_limit_remaining_tokens INTEGER
        CHECK (rate_limit_remaining_tokens IS NULL OR rate_limit_remaining_tokens >= 0),
    ADD COLUMN rate_limit_reset_requests TEXT,
    ADD COLUMN rate_limit_reset_tokens TEXT;

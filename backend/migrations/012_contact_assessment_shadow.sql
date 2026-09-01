-- An AI assessment is deliberately a review artifact, never a contact fact.
-- Message bodies stay in chat_messages; this ledger holds only IDs and the
-- model's small, validated suggestion for an owner to confirm manually.

CREATE TABLE contact_assessments (
    id UUID PRIMARY KEY,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    requested_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    source_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'invalid', 'cancelled')),
    model TEXT,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    input_char_count INTEGER NOT NULL DEFAULT 0 CHECK (input_char_count >= 0),
    response_id TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    result JSONB NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX contact_assessments_contact_created_idx
    ON contact_assessments (contact_id, created_at DESC);
CREATE INDEX contact_assessments_claim_idx
    ON contact_assessments (status, scheduled_at, created_at);
CREATE UNIQUE INDEX contact_assessments_one_active_contact_idx
    ON contact_assessments (contact_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE contact_assessment_refs (
    assessment_id UUID NOT NULL REFERENCES contact_assessments(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 24),
    authorization_id UUID NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('anchor', 'context', 'history')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (assessment_id, label),
    FOREIGN KEY (authorization_id, chat_id, message_id)
        REFERENCES authorization_message_observations(authorization_id, chat_id, message_id)
        ON DELETE CASCADE
);

CREATE INDEX contact_assessment_refs_message_idx
    ON contact_assessment_refs (authorization_id, chat_id, message_id);

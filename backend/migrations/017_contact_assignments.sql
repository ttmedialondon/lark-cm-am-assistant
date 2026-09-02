-- A creator can have one current CM/AM only.  Assignment history remains
-- append-only: a handover ends the current row and creates a new one.

-- OAuth health is not a job title. The owner explicitly enables this only for
-- real CM/AM accounts; new users are deliberately excluded until reviewed.
ALTER TABLE authorizations
    ADD COLUMN assignment_eligible BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE contact_assignment_assessments (
    id UUID PRIMARY KEY,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    requested_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'invalid', 'cancelled')),
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending', 'auto_assigned', 'review_required', 'no_action')),
    model TEXT,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    input_char_count INTEGER NOT NULL DEFAULT 0 CHECK (input_char_count >= 0),
    response_id TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    result JSONB NOT NULL DEFAULT '{}',
    error_message TEXT,
    source_message_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX contact_assignment_assessments_contact_created_idx
    ON contact_assignment_assessments (contact_id, created_at DESC);
CREATE INDEX contact_assignment_assessments_claim_idx
    ON contact_assignment_assessments (status, scheduled_at, created_at);
CREATE INDEX contact_assignment_assessments_contact_source_seen_idx
    ON contact_assignment_assessments (contact_id, source_message_seen_at DESC, created_at DESC);
CREATE UNIQUE INDEX contact_assignment_assessments_one_active_contact_idx
    ON contact_assignment_assessments (contact_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE contact_assignments (
    id UUID PRIMARY KEY,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    -- A deliberate hard deletion is a data reset, so remove its old assignment
    -- too rather than leaving a current row without a responsible person.
    responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('ai', 'manual')),
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR (confidence >= 0.000 AND confidence <= 1.000)),
    assessment_id UUID REFERENCES contact_assignment_assessments(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    ended_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE UNIQUE INDEX contact_assignments_one_active_contact_idx
    ON contact_assignments (contact_id)
    WHERE ended_at IS NULL;
CREATE INDEX contact_assignments_responsible_active_idx
    ON contact_assignments (responsible_authorization_id, started_at DESC)
    WHERE ended_at IS NULL;
CREATE INDEX contact_assignments_contact_history_idx
    ON contact_assignments (contact_id, started_at DESC);

-- Every reference is bound to the authorization that was allowed to observe
-- it.  This keeps evidence valid when the same chat is visible to many CM/AMs.
CREATE TABLE contact_assignment_assessment_refs (
    assessment_id UUID NOT NULL REFERENCES contact_assignment_assessments(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 24),
    candidate_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    authorization_id UUID NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('cm_am_message', 'creator_message')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (assessment_id, label),
    FOREIGN KEY (authorization_id, chat_id, message_id)
        REFERENCES authorization_message_observations(authorization_id, chat_id, message_id)
        ON DELETE CASCADE
);

CREATE INDEX contact_assignment_assessment_refs_message_idx
    ON contact_assignment_assessment_refs (authorization_id, chat_id, message_id);
CREATE INDEX contact_assignment_assessment_refs_candidate_idx
    ON contact_assignment_assessment_refs (candidate_authorization_id, assessment_id);

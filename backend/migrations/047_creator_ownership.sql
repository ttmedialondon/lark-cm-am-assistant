-- Second-step, person-level review. Never revive retired queues or change existing facts.
CREATE TABLE creator_ownership_runs (
    id UUID PRIMARY KEY,
    cm_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    requested_by UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('preparing','preview','running','paused','completed','cancelled')),
    directory_count INTEGER NOT NULL DEFAULT 0,
    excluded_owned_count INTEGER NOT NULL DEFAULT 0,
    excluded_identity_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX creator_ownership_one_open_run ON creator_ownership_runs(cm_authorization_id)
    WHERE status IN ('preparing','preview','running','paused');

CREATE TABLE creator_ownership_tasks (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES creator_ownership_runs(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
    cm_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('pending','preparing','ready','skipped','queued','running','succeeded','failed','invalid','cancelled')),
    reason_code TEXT NOT NULL DEFAULT '',
    fingerprint TEXT,
    config_fingerprint TEXT,
    pack_ciphertext TEXT,
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK(evidence_count>=0 AND evidence_count<=40),
    stats JSONB NOT NULL DEFAULT '{}',
    result_ciphertext TEXT,
    outcome TEXT CHECK(outcome IN ('suggested','uncertain','not_related')),
    confidence NUMERIC(4,3) CHECK(confidence>=0 AND confidence<=1),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending','confirmed','deferred')),
    reviewed_by UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    reused_from UUID REFERENCES creator_ownership_tasks(id) ON DELETE SET NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 2),
    api_requests INTEGER NOT NULL DEFAULT 0 CHECK(api_requests>=0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0),
    response_id TEXT,
    request_id TEXT,
    lease_until TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(run_id, contact_id)
);
CREATE INDEX creator_ownership_claim ON creator_ownership_tasks(status,scheduled_at,created_at);
CREATE INDEX creator_ownership_cached_result ON creator_ownership_tasks
    (contact_id,cm_authorization_id,policy_version,fingerprint,config_fingerprint)
    WHERE status='succeeded';
CREATE UNIQUE INDEX creator_ownership_one_paid_pair ON creator_ownership_tasks(contact_id,cm_authorization_id)
    WHERE status IN ('queued','running');

CREATE TABLE creator_ownership_refs (
    task_id UUID NOT NULL REFERENCES creator_ownership_tasks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    authorization_id UUID NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    metadata JSONB NOT NULL,
    PRIMARY KEY(task_id,label),
    FOREIGN KEY(authorization_id,chat_id,message_id)
        REFERENCES authorization_message_observations(authorization_id,chat_id,message_id) ON DELETE CASCADE
);
ALTER TABLE contact_assignments ADD COLUMN ownership_task_id UUID
    REFERENCES creator_ownership_tasks(id) ON DELETE SET NULL;

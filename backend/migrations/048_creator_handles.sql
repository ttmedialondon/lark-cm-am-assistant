-- Third-step handle review; preserve existing identities, assignments and account facts.
CREATE TABLE creator_handle_runs (
    id UUID PRIMARY KEY,
    ownership_run_id UUID NOT NULL UNIQUE REFERENCES creator_ownership_runs(id) ON DELETE RESTRICT,
    cm_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    requested_by UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preparing'
        CHECK (status IN ('preparing','preview','running','paused','completed')),
    auto_admit BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE creator_handle_tasks (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES creator_handle_runs(id) ON DELETE CASCADE,
    ownership_task_id UUID NOT NULL UNIQUE REFERENCES creator_ownership_tasks(id) ON DELETE RESTRICT,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
    cm_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','preparing','ready','queued','running','succeeded','skipped','failed','invalid')),
    reason_code TEXT NOT NULL DEFAULT '',
    fingerprint TEXT,
    config_fingerprint TEXT,
    pack_ciphertext TEXT,
    result_ciphertext TEXT,
    -- References contain identifiers and validation metadata only; private content stays encrypted in the pack.
    refs JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(refs)='array'),
    stats JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(stats)='object'),
    outcome TEXT CHECK (outcome IN ('matched','review','not_found','conflict')),
    confidence NUMERIC(4,3) CHECK (confidence>=0 AND confidence<=1),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','confirmed','deferred')),
    selected_handles TEXT[] NOT NULL DEFAULT '{}',
    reviewed_by UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    reused_from UUID REFERENCES creator_handle_tasks(id) ON DELETE SET NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 2),
    api_requests INTEGER NOT NULL DEFAULT 0 CHECK (api_requests>=0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens>=0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens>=0),
    response_id TEXT,
    request_id TEXT,
    lease_until TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX creator_handle_claim ON creator_handle_tasks(status,scheduled_at,created_at);
CREATE INDEX creator_handle_cached_result ON creator_handle_tasks
    (contact_id,cm_authorization_id,policy_version,fingerprint,config_fingerprint)
    WHERE status='succeeded';
CREATE UNIQUE INDEX creator_handle_one_paid_pair ON creator_handle_tasks(contact_id,cm_authorization_id)
    WHERE status IN ('queued','running');

ALTER TABLE contact_assignments ADD COLUMN handle_task_id UUID
    REFERENCES creator_handle_tasks(id) ON DELETE SET NULL;
ALTER TABLE contact_social_accounts ADD COLUMN handle_task_id UUID
    REFERENCES creator_handle_tasks(id) ON DELETE SET NULL;

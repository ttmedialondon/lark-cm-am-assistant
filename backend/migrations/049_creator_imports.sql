-- A new single-CM roster is explicit input, not an ownership-AI result.
CREATE TABLE creator_import_batches (
    id UUID PRIMARY KEY,
    cm_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    uploaded_by UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    filename TEXT NOT NULL,
    file_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preview' CHECK (status IN ('preview','running','paused','completed')),
    preview_rows JSONB NOT NULL DEFAULT '[]',
    counts JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(cm_authorization_id,uploaded_by,file_fingerprint,policy_version)
);
CREATE TABLE creator_import_rows (
    id UUID PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES creator_import_batches(id) ON DELETE CASCADE,
    cm_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    row_number INTEGER NOT NULL,
    original_handle TEXT NOT NULL,
    handle TEXT NOT NULL CHECK (length(handle) BETWEEN 1 AND 64),
    contact_id UUID REFERENCES contacts(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','preparing','queued','running','succeeded','skipped','failed','invalid')),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','confirmed','deferred')),
    reason_code TEXT NOT NULL DEFAULT '',
    outcome TEXT CHECK (outcome IN ('matched','review','not_found','conflict')),
    confidence NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    fingerprint TEXT,
    config_fingerprint TEXT,
    pack_ciphertext TEXT,
    result_ciphertext TEXT,
    refs JSONB NOT NULL DEFAULT '[]',
    stats JSONB NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 2),
    api_requests INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    response_id TEXT,
    request_id TEXT,
    lease_until TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(batch_id,handle),
    UNIQUE(batch_id,row_number)
);
CREATE INDEX creator_import_claim ON creator_import_rows(status,scheduled_at,created_at);
CREATE UNIQUE INDEX creator_import_one_paid_handle ON creator_import_rows(cm_authorization_id,handle)
    WHERE status IN ('queued','running');
ALTER TABLE contact_assignments ADD COLUMN import_row_id UUID REFERENCES creator_import_rows(id) ON DELETE SET NULL;
ALTER TABLE contact_social_accounts
    ADD COLUMN import_row_id UUID REFERENCES creator_import_rows(id) ON DELETE SET NULL;

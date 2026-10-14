-- Derived attachment text only. Original resource bytes never enter this queue.
CREATE TABLE message_material_jobs (
    id UUID PRIMARY KEY,
    actor_id UUID NOT NULL REFERENCES authorizations(id),
    cm_id UUID NOT NULL REFERENCES authorizations(id),
    feature TEXT NOT NULL CHECK (feature IN ('operations_assistant','offhours_summary')),
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    source_version TEXT NOT NULL CHECK (source_version ~ '^[0-9a-f]{64}$'),
    consent_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','succeeded','failed','unknown','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
    lease_until TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checkpoint_ciphertext TEXT,
    result_ciphertext TEXT,
    usage JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(usage)='object'),
    response_id TEXT,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (chat_id,message_id) REFERENCES chat_messages(chat_id,message_id),
    UNIQUE (cm_id,chat_id,message_id,source_version,consent_at),
    CHECK ((status='running')=(lease_until IS NOT NULL))
);
CREATE INDEX message_material_claim_idx ON message_material_jobs(scheduled_at,created_at) WHERE status='queued';
CREATE UNIQUE INDEX message_material_one_running_idx ON message_material_jobs((TRUE)) WHERE status='running';

-- Drafts never send Lark messages or change historical event status.
CREATE TABLE summary_reply_tasks (
    id UUID PRIMARY KEY,
    actor_id UUID NOT NULL REFERENCES authorizations(id),
    summary_task_id UUID NOT NULL REFERENCES offhours_summary_tasks(id),
    event_index INTEGER NOT NULL CHECK(event_index >= 0),
    action TEXT NOT NULL CHECK(action IN ('generate','translate')),
    parent_id UUID REFERENCES summary_reply_tasks(id),
    input_ciphertext TEXT,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','stale')),
    checkpoint_ciphertext TEXT,
    result_ciphertext TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_message TEXT
);
CREATE UNIQUE INDEX summary_reply_active_request ON summary_reply_tasks(actor_id,request_hash)
    WHERE status IN ('queued','running');

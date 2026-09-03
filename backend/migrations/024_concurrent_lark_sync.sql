-- Keep one coordinator job per authorization, but let a worker safely fetch
-- different conversations in parallel.  A claim is fenced by its UUID and
-- the existing sync_generation, so an OAuth reauthorization can invalidate an
-- in-flight response without letting it advance an old cursor.

ALTER TABLE sync_jobs
    ADD COLUMN discovery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (discovery_status IN ('pending', 'running', 'succeeded')),
    ADD COLUMN discovery_generation INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN discovery_completed_at TIMESTAMPTZ,
    ADD COLUMN last_progress_at TIMESTAMPTZ,
    ADD COLUMN last_http_status INTEGER;

ALTER TABLE authorization_chats
    ADD COLUMN page_claim_id UUID,
    ADD COLUMN page_claim_job_id UUID,
    ADD COLUMN page_claim_mode TEXT
        CHECK (page_claim_mode IN ('backfill', 'incremental')),
    ADD COLUMN page_claimed_at TIMESTAMPTZ,
    ADD COLUMN page_lease_until TIMESTAMPTZ,
    ADD COLUMN page_retry_at TIMESTAMPTZ,
    ADD COLUMN member_snapshot_claim_id UUID,
    ADD COLUMN member_snapshot_claimed_at TIMESTAMPTZ,
    ADD COLUMN member_snapshot_lease_until TIMESTAMPTZ,
    ADD COLUMN member_snapshot_retry_at TIMESTAMPTZ;

CREATE INDEX authorization_chats_page_claim_idx
    ON authorization_chats (page_lease_until)
    WHERE page_claim_id IS NOT NULL;

CREATE INDEX authorization_chats_page_retry_idx
    ON authorization_chats (page_retry_at)
    WHERE page_retry_at IS NOT NULL;

CREATE INDEX authorization_chats_member_snapshot_claim_idx
    ON authorization_chats (member_snapshot_lease_until)
    WHERE member_snapshot_claim_id IS NOT NULL;

CREATE INDEX authorization_chats_member_snapshot_retry_idx
    ON authorization_chats (member_snapshot_retry_at)
    WHERE member_snapshot_retry_at IS NOT NULL;

-- A release can otherwise resume an old job after upgrading this worker.  It
-- is safe (and intentionally one-off) to repeat discovery before its pages
-- are dispatched by the new scheduler.
UPDATE sync_jobs
SET status = 'queued', lease_until = NULL,
    discovery_status = 'pending', discovery_generation = discovery_generation + 1,
    discovery_completed_at = NULL, updated_at = NOW()
WHERE status IN ('queued', 'running');

-- PostgreSQL is the shared admission point for every worker process.  The
-- short request leases enforce the global in-flight limit; the state row
-- serializes rate reservations and an API-wide pause after a 429.
CREATE TABLE lark_sync_admission_state (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    next_request_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paused_until TIMESTAMPTZ,
    last_rate_limited_at TIMESTAMPTZ,
    rate_limited_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO lark_sync_admission_state (id) VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE lark_sync_request_leases (
    id UUID PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX lark_sync_request_leases_expiry_idx
    ON lark_sync_request_leases (expires_at);

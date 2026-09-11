-- One cache/lease per stable Lark user, never one scan per CM.
ALTER TABLE lark_identities
    ADD COLUMN avatar_url TEXT,
    ADD COLUMN avatar_updated_at TIMESTAMPTZ,
    ADD COLUMN avatar_checked_at TIMESTAMPTZ,
    ADD COLUMN avatar_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN avatar_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (avatar_status IN ('pending','running','succeeded','unavailable','failed')),
    ADD COLUMN avatar_error_code TEXT,
    ADD COLUMN avatar_claim_id UUID;
CREATE INDEX lark_identities_avatar_due_idx ON lark_identities(avatar_retry_at,open_id)
    WHERE identity_type='user';

CREATE TABLE identity_profile_providers (
    authorization_id UUID PRIMARY KEY REFERENCES authorizations(id) ON DELETE CASCADE,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','checking','available','unavailable','failed')),
    checked_at TIMESTAMPTZ,
    retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_code TEXT
);

-- Avatar starts have their own cadence; do not slow the shared message gate.
ALTER TABLE lark_sync_admission_state
    ADD COLUMN avatar_next_request_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

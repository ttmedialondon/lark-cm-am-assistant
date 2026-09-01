-- Stable Lark identity facts are deliberately separate from OAuth grants:
-- an external participant may never authorize this application themselves.

CREATE TABLE lark_identities (
    open_id TEXT PRIMARY KEY,
    tenant_key TEXT,
    display_name TEXT,
    identity_type TEXT NOT NULL DEFAULT 'user',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO lark_identities (open_id, display_name)
SELECT open_id, display_name
FROM authorizations
ON CONFLICT (open_id) DO UPDATE SET
    display_name = COALESCE(EXCLUDED.display_name, lark_identities.display_name),
    last_seen_at = NOW();

ALTER TABLE chat_messages
    ADD COLUMN sender_id TEXT,
    ADD COLUMN sender_id_type TEXT,
    ADD COLUMN sender_open_id TEXT REFERENCES lark_identities(open_id) ON DELETE SET NULL,
    ADD COLUMN sender_tenant_key TEXT,
    ADD COLUMN sender_type TEXT;

CREATE INDEX chat_messages_sender_open_id_time_idx
    ON chat_messages (sender_open_id, message_create_time DESC)
    WHERE sender_open_id IS NOT NULL;

ALTER TABLE authorization_chats
    ADD COLUMN member_snapshot_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (member_snapshot_status IN ('pending', 'succeeded', 'partial', 'unavailable')),
    ADD COLUMN member_snapshot_checked_at TIMESTAMPTZ,
    ADD COLUMN p2p_counterpart_id TEXT,
    ADD COLUMN p2p_counterpart_type TEXT,
    ADD COLUMN p2p_counterpart_open_id TEXT REFERENCES lark_identities(open_id) ON DELETE SET NULL,
    ADD COLUMN p2p_counterpart_seen_at TIMESTAMPTZ;

CREATE INDEX authorization_chats_member_snapshot_refresh_idx
    ON authorization_chats (authorization_id, member_snapshot_checked_at);

CREATE INDEX authorization_chats_p2p_counterpart_idx
    ON authorization_chats (p2p_counterpart_open_id)
    WHERE p2p_counterpart_open_id IS NOT NULL;

-- This is the latest roster fact, not a membership-history ledger.  An omitted
-- person is never treated as having left because Lark may return a truncated view.
CREATE TABLE chat_members (
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    open_id TEXT NOT NULL REFERENCES lark_identities(open_id) ON DELETE RESTRICT,
    tenant_key TEXT,
    display_name TEXT,
    member_type TEXT NOT NULL DEFAULT 'user',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_observed_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    PRIMARY KEY (chat_id, open_id)
);

CREATE INDEX chat_members_open_id_idx ON chat_members (open_id, chat_id);

CREATE TABLE creators (
    id UUID PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 160),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX creators_name_idx ON creators (lower(display_name), id);

CREATE TABLE creator_identifiers (
    id UUID PRIMARY KEY,
    creator_id UUID NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('handle', 'email', 'phone', 'crm_id', 'alias')),
    platform TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) > 0),
    display_value TEXT NOT NULL CHECK (length(btrim(display_value)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (creator_id, kind, platform, normalized_value)
);

CREATE UNIQUE INDEX creator_identifiers_handle_unique_idx
    ON creator_identifiers (platform, normalized_value)
    WHERE kind = 'handle';

CREATE UNIQUE INDEX creator_identifiers_crm_unique_idx
    ON creator_identifiers (normalized_value)
    WHERE kind = 'crm_id';

CREATE TABLE creator_identity_candidates (
    id UUID PRIMARY KEY,
    subject_open_id TEXT REFERENCES lark_identities(open_id) ON DELETE RESTRICT,
    creator_id UUID REFERENCES creators(id) ON DELETE SET NULL,
    identifier_kind TEXT NOT NULL CHECK (identifier_kind IN ('handle', 'email', 'phone', 'crm_id', 'alias')),
    platform TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) > 0),
    display_value TEXT NOT NULL CHECK (length(btrim(display_value)) > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'rejected', 'conflict')),
    source TEXT NOT NULL DEFAULT 'rule',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL
);

CREATE INDEX creator_identity_candidates_subject_idx
    ON creator_identity_candidates (subject_open_id, status, created_at DESC)
    WHERE subject_open_id IS NOT NULL;

CREATE INDEX creator_identity_candidates_creator_idx
    ON creator_identity_candidates (creator_id, status, created_at DESC)
    WHERE creator_id IS NOT NULL;

CREATE TABLE creator_candidate_evidence (
    id UUID PRIMARY KEY,
    candidate_id UUID NOT NULL REFERENCES creator_identity_candidates(id) ON DELETE CASCADE,
    chat_id TEXT REFERENCES chats(chat_id) ON DELETE SET NULL,
    message_id TEXT,
    source_open_id TEXT REFERENCES lark_identities(open_id) ON DELETE SET NULL,
    observed_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('message', 'member', 'p2p', 'crm', 'manual')),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (message_id IS NULL OR chat_id IS NOT NULL),
    FOREIGN KEY (chat_id, message_id)
        REFERENCES chat_messages(chat_id, message_id) ON DELETE SET NULL
);

CREATE INDEX creator_candidate_evidence_candidate_idx
    ON creator_candidate_evidence (candidate_id, created_at);

CREATE INDEX creator_candidate_evidence_source_idx
    ON creator_candidate_evidence (source_open_id, created_at DESC)
    WHERE source_open_id IS NOT NULL;

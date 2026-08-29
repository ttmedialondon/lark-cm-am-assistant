-- Normalize the original per-authorization prototype before more CM/AM users
-- are onboarded.  This migration is intentionally lossless: every existing
-- ledger record becomes one canonical message plus one authorization-specific
-- observation, then the redundant ledger is removed in the same transaction.

ALTER TABLE user_chats RENAME TO authorization_chats;

CREATE TABLE chats (
    chat_id TEXT PRIMARY KEY,
    name TEXT,
    external BOOLEAN NOT NULL DEFAULT FALSE,
    chat_status TEXT,
    chat_mode TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

WITH chat_bounds AS (
    SELECT
        chat_id,
        MIN(discovered_at) AS first_seen_at,
        MAX(last_seen_at) AS last_seen_at
    FROM authorization_chats
    GROUP BY chat_id
),
latest_metadata AS (
    SELECT DISTINCT ON (chat_id)
        chat_id, name, external, chat_status, chat_mode, last_seen_at
    FROM authorization_chats
    ORDER BY chat_id, last_seen_at DESC, discovered_at DESC
)
INSERT INTO chats (
    chat_id, name, external, chat_status, chat_mode,
    first_seen_at, last_seen_at, metadata_updated_at
)
SELECT
    latest_metadata.chat_id,
    latest_metadata.name,
    latest_metadata.external,
    latest_metadata.chat_status,
    latest_metadata.chat_mode,
    chat_bounds.first_seen_at,
    chat_bounds.last_seen_at,
    latest_metadata.last_seen_at
FROM latest_metadata
JOIN chat_bounds USING (chat_id);

ALTER TABLE authorization_chats
    ADD CONSTRAINT authorization_chats_chat_id_fkey
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE RESTRICT;

ALTER TABLE authorization_chats
    DROP COLUMN name,
    DROP COLUMN external,
    DROP COLUMN chat_status,
    DROP COLUMN chat_mode;

CREATE INDEX authorization_chats_visible_chat_idx
    ON authorization_chats (chat_id, authorization_id)
    WHERE is_visible AND sync_enabled;

CREATE TABLE chat_messages (
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE RESTRICT,
    message_id TEXT NOT NULL,
    message_create_time TIMESTAMPTZ,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    first_indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE authorization_message_observations (
    authorization_id UUID NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sent_by_author BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (authorization_id, chat_id, message_id),
    FOREIGN KEY (authorization_id, chat_id)
        REFERENCES authorization_chats (authorization_id, chat_id) ON DELETE CASCADE,
    FOREIGN KEY (chat_id, message_id)
        REFERENCES chat_messages (chat_id, message_id) ON DELETE RESTRICT
);

INSERT INTO chat_messages (
    chat_id, message_id, message_create_time, is_deleted,
    first_indexed_at, last_observed_at
)
SELECT
    chat_id,
    message_id,
    MIN(message_create_time),
    BOOL_OR(is_deleted),
    MIN(indexed_at),
    MAX(indexed_at)
FROM message_ledger
GROUP BY chat_id, message_id;

INSERT INTO authorization_message_observations (
    authorization_id, chat_id, message_id, sent_by_author, observed_at
)
SELECT
    authorization_id,
    chat_id,
    message_id,
    sent_by_author,
    indexed_at
FROM message_ledger;

DROP TABLE message_ledger;

CREATE INDEX chat_messages_live_chat_time_idx
    ON chat_messages (chat_id, message_create_time)
    WHERE NOT is_deleted;

CREATE INDEX authorization_message_observations_author_idx
    ON authorization_message_observations (authorization_id, chat_id);

CREATE INDEX sync_jobs_authorization_created_idx
    ON sync_jobs (authorization_id, created_at DESC);

CREATE TABLE admin_roles (
    authorization_id UUID PRIMARY KEY REFERENCES authorizations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'operator', 'viewer')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    granted_by UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

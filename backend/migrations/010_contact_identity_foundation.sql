-- Replace the old inference-shaped candidate store with durable facts:
-- contacts, independent social accounts, and message-level account observations.
-- This migration intentionally never infers ownership from a message sender.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM creator_identity_candidates
        WHERE identifier_kind <> 'handle' OR platform = ''
    ) THEN
        RAISE EXCEPTION 'legacy creator candidates cannot be converted to social accounts';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM creator_candidate_evidence
        WHERE evidence_type <> 'message'
           OR chat_id IS NULL
           OR message_id IS NULL
           OR observed_by_authorization_id IS NULL
    ) THEN
        RAISE EXCEPTION 'legacy candidate evidence cannot be converted to message observations';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM creator_candidate_evidence evidence
        JOIN creator_identity_candidates candidate ON candidate.id = evidence.candidate_id
        GROUP BY
            candidate.platform,
            candidate.normalized_value,
            evidence.observed_by_authorization_id,
            evidence.chat_id,
            evidence.message_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'legacy candidate evidence would collide after conversion';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM creator_candidate_evidence evidence
        LEFT JOIN authorization_message_observations observation
          ON observation.authorization_id = evidence.observed_by_authorization_id
         AND observation.chat_id = evidence.chat_id
         AND observation.message_id = evidence.message_id
        WHERE observation.authorization_id IS NULL
    ) THEN
        RAISE EXCEPTION 'legacy candidate evidence is outside its authorization scope';
    END IF;
END $$;

CREATE TABLE contacts (
    id UUID PRIMARY KEY,
    display_name TEXT,
    entity_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (entity_type IN ('person', 'organization', 'unknown')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE lark_identities
    ADD COLUMN contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
    ADD COLUMN tenant_classification TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (tenant_classification IN ('internal', 'known_external', 'unresolved'));

CREATE INDEX lark_identities_contact_idx ON lark_identities (contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX lark_identities_contact_tenant_idx
    ON lark_identities (tenant_classification, contact_id) WHERE contact_id IS NOT NULL;

CREATE TABLE contact_roles (
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('creator', 'agent_or_agency', 'brand', 'merchant', 'supplier', 'other')),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'rejected')),
    source TEXT NOT NULL,
    reviewed_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contact_id, role)
);

CREATE TABLE contact_identifiers (
    id UUID PRIMARY KEY,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('email', 'phone', 'crm_id', 'alias')),
    platform TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) > 0),
    display_value TEXT NOT NULL CHECK (length(btrim(display_value)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (contact_id, kind, platform, normalized_value)
);

CREATE UNIQUE INDEX contact_identifiers_crm_unique_idx
    ON contact_identifiers (normalized_value) WHERE kind = 'crm_id';

CREATE TABLE social_accounts (
    id UUID PRIMARY KEY,
    platform TEXT NOT NULL CHECK (length(btrim(platform)) BETWEEN 1 AND 40),
    normalized_handle TEXT NOT NULL CHECK (length(btrim(normalized_handle)) BETWEEN 1 AND 320),
    display_handle TEXT NOT NULL CHECK (length(btrim(display_handle)) BETWEEN 1 AND 320),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, normalized_handle)
);

CREATE TABLE contact_social_accounts (
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    social_account_id UUID NOT NULL REFERENCES social_accounts(id) ON DELETE RESTRICT,
    relationship TEXT NOT NULL CHECK (relationship IN ('owner', 'representative')),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'rejected')),
    source TEXT NOT NULL,
    reviewed_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contact_id, social_account_id, relationship)
);

CREATE UNIQUE INDEX contact_social_accounts_one_owner_idx
    ON contact_social_accounts (social_account_id)
    WHERE relationship = 'owner' AND status = 'confirmed';

CREATE TABLE social_account_observations (
    id UUID PRIMARY KEY,
    social_account_id UUID NOT NULL REFERENCES social_accounts(id) ON DELETE RESTRICT,
    source_open_id TEXT REFERENCES lark_identities(open_id) ON DELETE SET NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    observed_by_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    extractor TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (social_account_id, observed_by_authorization_id, chat_id, message_id),
    FOREIGN KEY (chat_id, message_id)
        REFERENCES chat_messages(chat_id, message_id) ON DELETE CASCADE,
    FOREIGN KEY (observed_by_authorization_id, chat_id, message_id)
        REFERENCES authorization_message_observations(authorization_id, chat_id, message_id)
        ON DELETE CASCADE
);

CREATE INDEX social_account_observations_source_idx
    ON social_account_observations (source_open_id, created_at DESC) WHERE source_open_id IS NOT NULL;
CREATE INDEX social_account_observations_account_idx
    ON social_account_observations (social_account_id, created_at DESC);

-- A legacy P2P bot was previously written as a user identity. Correct only
-- identities that have no independent user/member/message fact.
UPDATE lark_identities identity
SET identity_type = 'bot'
WHERE identity.identity_type = 'user'
  AND EXISTS (
      SELECT 1
      FROM authorization_chats access
      WHERE access.p2p_counterpart_type = 'bot'
        AND access.p2p_counterpart_id = identity.open_id
  )
  AND NOT EXISTS (SELECT 1 FROM chat_members member WHERE member.open_id = identity.open_id)
  AND NOT EXISTS (SELECT 1 FROM chat_messages message WHERE message.sender_open_id = identity.open_id);

WITH internal_tenants AS (
    SELECT DISTINCT tenant_key
    FROM (
        SELECT identity.tenant_key
        FROM authorizations auth
        JOIN lark_identities identity ON identity.open_id = auth.open_id
        WHERE identity.tenant_key IS NOT NULL
        UNION
        SELECT message.sender_tenant_key
        FROM authorizations auth
        JOIN chat_messages message ON message.sender_open_id = auth.open_id
        WHERE message.sender_tenant_key IS NOT NULL
    ) tenant_facts
)
UPDATE lark_identities identity
SET tenant_classification = CASE
    WHEN identity.identity_type <> 'user' THEN 'internal'
    WHEN identity.tenant_key IS NULL THEN 'unresolved'
    WHEN EXISTS (SELECT 1 FROM internal_tenants WHERE tenant_key = identity.tenant_key) THEN 'internal'
    ELSE 'known_external'
END;

INSERT INTO contacts (id, display_name, entity_type, created_at, updated_at)
SELECT id, display_name, 'unknown', created_at, updated_at
FROM creators;

INSERT INTO contact_roles (contact_id, role, status, source, created_at, updated_at)
SELECT id, 'creator', 'confirmed', 'legacy_creator_registry', created_at, updated_at
FROM creators;

CREATE TEMP TABLE contact_identity_seed (
    open_id TEXT PRIMARY KEY,
    contact_id UUID NOT NULL
) ON COMMIT DROP;

INSERT INTO contact_identity_seed (open_id, contact_id)
SELECT open_id, gen_random_uuid()
FROM lark_identities
WHERE identity_type = 'user';

INSERT INTO contacts (id, display_name, entity_type, created_at, updated_at)
SELECT seed.contact_id, identity.display_name, 'person', identity.first_seen_at, identity.last_seen_at
FROM contact_identity_seed seed
JOIN lark_identities identity ON identity.open_id = seed.open_id;

UPDATE lark_identities identity
SET contact_id = seed.contact_id
FROM contact_identity_seed seed
WHERE identity.open_id = seed.open_id;

INSERT INTO contact_identifiers (
    id, contact_id, kind, platform, normalized_value, display_value, created_at, updated_at
)
SELECT id, creator_id, kind, platform, normalized_value, display_value, created_at, updated_at
FROM creator_identifiers
WHERE kind <> 'handle';

INSERT INTO social_accounts (
    id, platform, normalized_handle, display_handle, created_at, updated_at
)
SELECT id, platform, normalized_value, display_value, created_at, updated_at
FROM creator_identifiers
WHERE kind = 'handle'
ON CONFLICT (platform, normalized_handle) DO UPDATE SET
    display_handle = EXCLUDED.display_handle,
    updated_at = GREATEST(social_accounts.updated_at, EXCLUDED.updated_at);

INSERT INTO social_accounts (
    id, platform, normalized_handle, display_handle, created_at, updated_at
)
SELECT DISTINCT ON (platform, normalized_value)
    id, platform, normalized_value, display_value, created_at, updated_at
FROM creator_identity_candidates
ORDER BY platform, normalized_value, created_at, id
ON CONFLICT (platform, normalized_handle) DO UPDATE SET
    display_handle = EXCLUDED.display_handle,
    updated_at = GREATEST(social_accounts.updated_at, EXCLUDED.updated_at);

INSERT INTO contact_social_accounts (
    contact_id, social_account_id, relationship, status, source, created_at, updated_at
)
SELECT identifier.creator_id, account.id, 'owner', 'confirmed', 'legacy_creator_registry',
       identifier.created_at, identifier.updated_at
FROM creator_identifiers identifier
JOIN social_accounts account
  ON account.platform = identifier.platform AND account.normalized_handle = identifier.normalized_value
WHERE identifier.kind = 'handle';

INSERT INTO social_account_observations (
    id, social_account_id, source_open_id, chat_id, message_id,
    observed_by_authorization_id, extractor, metadata, created_at, last_observed_at
)
SELECT
    evidence.id,
    account.id,
    evidence.source_open_id,
    evidence.chat_id,
    evidence.message_id,
    evidence.observed_by_authorization_id,
    candidate.source,
    jsonb_build_object(
        'migrated_from', 'creator_candidate_evidence',
        'legacy_candidate_id', candidate.id::text,
        'legacy_source', candidate.source,
        'legacy_status', candidate.status,
        'observed_display_handle', candidate.display_value,
        'legacy_evidence_metadata', evidence.metadata
    ),
    evidence.created_at,
    evidence.created_at
FROM creator_candidate_evidence evidence
JOIN creator_identity_candidates candidate ON candidate.id = evidence.candidate_id
JOIN social_accounts account
  ON account.platform = candidate.platform AND account.normalized_handle = candidate.normalized_value;

UPDATE authorization_chats
SET p2p_counterpart_open_id = NULL,
    p2p_counterpart_seen_at = NULL
WHERE p2p_counterpart_type IS DISTINCT FROM 'user';

DROP TABLE creator_candidate_evidence;
DROP TABLE creator_identity_candidates;
DROP TABLE creator_identifiers;
DROP TABLE creators;

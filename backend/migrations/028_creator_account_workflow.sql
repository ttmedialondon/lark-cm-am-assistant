-- Additive only: legacy contact-level assignments and identity results stay untouched.
-- Migration 016 removed the earlier account tables; restore their minimal durable
-- identity shape, without reviving observations or importing historical guesses.
CREATE TABLE social_accounts (
    id UUID PRIMARY KEY,
    platform TEXT NOT NULL CHECK (length(btrim(platform)) BETWEEN 1 AND 40),
    normalized_handle TEXT NOT NULL CHECK (length(btrim(normalized_handle)) BETWEEN 1 AND 320),
    display_handle TEXT NOT NULL CHECK (length(btrim(display_handle)) BETWEEN 1 AND 320),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(platform, normalized_handle)
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
    PRIMARY KEY(contact_id, social_account_id, relationship)
);
CREATE UNIQUE INDEX contact_social_accounts_one_owner_idx ON contact_social_accounts(social_account_id)
    WHERE relationship='owner' AND status='confirmed';

CREATE TABLE creator_workflow_candidates (
    id UUID PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'tiktok' CHECK (length(platform) BETWEEN 1 AND 40),
    normalized_handle TEXT CHECK (normalized_handle IS NULL OR length(normalized_handle) BETWEEN 1 AND 160),
    responsible_authorization_id UUID REFERENCES authorizations(id) ON DELETE RESTRICT,
    contact_id UUID REFERENCES contacts(id) ON DELETE RESTRICT,
    relationship TEXT CHECK (relationship IN ('owner', 'representative')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'deferred', 'rejected', 'confirmed', 'merged')),
    uncertainties TEXT[] NOT NULL DEFAULT '{}',
    social_account_id UUID REFERENCES social_accounts(id) ON DELETE RESTRICT,
    merged_into UUID REFERENCES creator_workflow_candidates(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (status <> 'confirmed' OR (normalized_handle IS NOT NULL
        AND responsible_authorization_id IS NOT NULL AND contact_id IS NOT NULL
        AND social_account_id IS NOT NULL AND relationship IS NOT NULL AND cardinality(uncertainties) = 0)),
    CHECK ((status = 'merged') = (merged_into IS NOT NULL))
);
CREATE UNIQUE INDEX creator_workflow_account_candidate_idx
    ON creator_workflow_candidates(platform, normalized_handle)
    WHERE normalized_handle IS NOT NULL AND merged_into IS NULL AND status <> 'confirmed';
CREATE INDEX creator_workflow_responsible_idx
    ON creator_workflow_candidates(responsible_authorization_id, status, updated_at DESC);

CREATE TABLE creator_workflow_sources (
    id UUID PRIMARY KEY,
    candidate_id UUID NOT NULL REFERENCES creator_workflow_candidates(id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL CHECK (source_type IN ('excel', 'ai')),
    source_key TEXT NOT NULL CHECK (length(source_key) BETWEEN 1 AND 500),
    source_data JSONB NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    submitted_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_type, source_key, fingerprint)
);
CREATE INDEX creator_workflow_source_candidate_idx ON creator_workflow_sources(candidate_id);

CREATE TABLE creator_account_assignments (
    id UUID PRIMARY KEY,
    social_account_id UUID NOT NULL REFERENCES social_accounts(id) ON DELETE RESTRICT,
    responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
    relationship TEXT NOT NULL CHECK (relationship IN ('owner', 'representative')),
    source TEXT NOT NULL CHECK (source IN ('cm_confirmation', 'excel_verified')),
    candidate_id UUID NOT NULL REFERENCES creator_workflow_candidates(id) ON DELETE RESTRICT,
    created_by_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX creator_account_one_current_cm_idx
    ON creator_account_assignments(social_account_id) WHERE ended_at IS NULL;
CREATE INDEX creator_account_current_cm_idx
    ON creator_account_assignments(responsible_authorization_id) WHERE ended_at IS NULL;

-- Exact request replays succeed, but reusing a key for a different mutation does not.
CREATE TABLE creator_workflow_requests (
    actor_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    request_id UUID NOT NULL,
    fingerprint TEXT NOT NULL,
    candidate_id UUID NOT NULL REFERENCES creator_workflow_candidates(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(actor_id, request_id)
);

-- Keep Lark source facts intact, but replace the old role taxonomy and all
-- derived AI decisions. Existing messages/identities are the evidence for a
-- clean re-evaluation; they must not be deleted just to change a classifier.

CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL CHECK (length(btrim(canonical_name)) BETWEEN 1 AND 320),
    organization_kind TEXT NOT NULL DEFAULT 'unknown'
        CHECK (organization_kind IN ('unknown', 'mcn_or_agency', 'merchant_or_brand', 'platform', 'partner_service', 'other')),
    verification_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (verification_status IN ('verified', 'candidate', 'unknown')),
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('lark_trust_party', 'crm', 'manual')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX organizations_canonical_name_unique_idx
    ON organizations (LOWER(canonical_name));

CREATE TABLE organization_tenants (
    tenant_key TEXT PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('lark_trust_party', 'crm', 'manual')),
    status TEXT NOT NULL CHECK (status IN ('verified', 'rejected')),
    verified_at TIMESTAMPTZ,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX organization_tenants_organization_idx ON organization_tenants (organization_id);

CREATE TABLE contact_organization_links (
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL DEFAULT 'tenant_affiliation'
        CHECK (relationship IN ('tenant_affiliation', 'employee', 'representative', 'agent', 'owner', 'unknown')),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('verified', 'candidate', 'rejected')),
    source TEXT NOT NULL CHECK (source IN ('lark_trust_party', 'crm', 'manual', 'ai_candidate')),
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR (confidence >= 0.000 AND confidence <= 1.000)),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contact_id, organization_id, relationship)
);

CREATE INDEX contact_organization_links_contact_idx
    ON contact_organization_links (contact_id, status, last_seen_at DESC);

CREATE INDEX lark_identities_tenant_contact_idx
    ON lark_identities (tenant_key)
    WHERE contact_id IS NOT NULL;

CREATE TABLE organization_sync_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'unavailable', 'failed')),
    checked_at TIMESTAMPTZ,
    succeeded_at TIMESTAMPTZ,
    detail TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO organization_sync_state (singleton) VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

-- New main category vocabulary. A contact may retain supporting facts later,
-- but exactly one confirmed primary category is shown to operators.
ALTER TABLE contact_roles
    ADD COLUMN is_primary BOOLEAN NOT NULL DEFAULT FALSE;

DELETE FROM contact_assessments;
DELETE FROM contact_assignment_assessments;
DELETE FROM contact_roles;

ALTER TABLE contact_roles DROP CONSTRAINT IF EXISTS contact_roles_role_check;
ALTER TABLE contact_roles
    ADD CONSTRAINT contact_roles_role_check CHECK (
        role IN (
            'creator',
            'partner_contact',
            'internal_colleague',
            'merchant_contact',
            'platform_contact',
            'other_external_contact'
        )
    );

CREATE UNIQUE INDEX contact_roles_one_primary_confirmed_idx
    ON contact_roles (contact_id)
    WHERE status = 'confirmed' AND is_primary;

-- Internal membership is a Lark tenant fact, never an AI guess. It becomes
-- the initial primary category while all external contacts remain unclassified
-- until the new evidence-bound queue evaluates them.
INSERT INTO contact_roles (contact_id, role, status, source, is_primary, reviewed_at)
SELECT DISTINCT identity.contact_id, 'internal_colleague', 'confirmed', 'lark_internal_tenant', TRUE, NOW()
FROM lark_identities identity
WHERE identity.identity_type = 'user'
  AND identity.tenant_classification = 'internal'
  AND identity.contact_id IS NOT NULL;

-- Stop old derived work and force a fresh discovery/member pass. Full message
-- history remains in place, so we do not issue an unnecessary costly history
-- fetch solely to reclassify people.
UPDATE sync_jobs
SET status = 'cancelled', finished_at = NOW(), lease_until = NULL,
    last_error = '联系人身份分类已重置；将按新的组织与证据规则重新同步。',
    updated_at = NOW()
WHERE status IN ('queued', 'running');

UPDATE authorization_chats
SET member_count = NULL,
    member_count_status = 'pending',
    member_count_checked_at = NULL,
    member_snapshot_status = 'pending',
    member_snapshot_checked_at = NULL;

INSERT INTO sync_jobs (id, authorization_id, mode, status, scheduled_at)
SELECT gen_random_uuid(), auth.id, 'incremental', 'queued', NOW()
FROM authorizations auth
WHERE auth.status = 'active'
ON CONFLICT (authorization_id) WHERE status IN ('queued', 'running') DO NOTHING;

UPDATE ai_feature_configs
SET automatic_queue_checked_at = '-infinity'::timestamptz,
    updated_at = NOW()
WHERE feature IN ('contact_assessment', 'contact_assignment');

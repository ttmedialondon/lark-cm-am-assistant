-- Personal reusable message drafts only. No sending or external permissions are enabled.
CREATE TABLE creator_broadcast_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000 AND length(btrim(body)) > 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX creator_broadcast_templates_owner_active
    ON creator_broadcast_templates(owner_authorization_id,updated_at DESC,id) WHERE deleted_at IS NULL;

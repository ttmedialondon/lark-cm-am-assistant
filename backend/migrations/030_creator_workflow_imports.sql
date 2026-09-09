-- Previews are server-owned, short-lived snapshots, never client-supplied authority.
CREATE TABLE creator_workflow_import_batches (
    id UUID PRIMARY KEY,
    actor_id UUID NOT NULL REFERENCES authorizations(id),
    responsible_id UUID REFERENCES authorizations(id),
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    rows JSONB NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '2 hours'
);
CREATE INDEX creator_workflow_import_actor_idx ON creator_workflow_import_batches(actor_id, created_at DESC);
CREATE TABLE creator_workflow_cm_aliases (
    alias TEXT PRIMARY KEY,
    responsible_id UUID NOT NULL REFERENCES authorizations(id)
);
-- This established business alias was explicitly identified by the owner.
INSERT INTO creator_workflow_cm_aliases(alias,responsible_id)
SELECT 'remi',id FROM authorizations WHERE id='24d1ef13-3762-4359-ba11-d26c03cf1f7f';

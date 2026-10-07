-- Personal, static ID targets only. No message body or inferred relationship is saved.
CREATE TABLE creator_broadcast_lists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 500),
    cm_id UUID REFERENCES authorizations(id) ON DELETE RESTRICT,
    purpose TEXT NOT NULL CHECK (purpose IN ('support','community','training','category','campaign')),
    mention BOOLEAN NOT NULL DEFAULT TRUE,
    channel TEXT NOT NULL DEFAULT 'group' CHECK (channel='group'),
    targets JSONB NOT NULL CHECK (jsonb_typeof(targets)='array'
        AND jsonb_array_length(targets) BETWEEN 1 AND 500),
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX creator_broadcast_lists_owner_active
    ON creator_broadcast_lists(owner_authorization_id,updated_at DESC,id) WHERE deleted_at IS NULL;

-- No implicit grants: existing non-owner roles remain front-end only until the owner selects pages.
CREATE TABLE admin_page_permissions (
    authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK (permission IN (
        'overview', 'users', 'chats', 'messages', 'contacts', 'creators', 'creator-review', 'ai', 'jobs'
    )),
    granted_by UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (authorization_id, permission)
);

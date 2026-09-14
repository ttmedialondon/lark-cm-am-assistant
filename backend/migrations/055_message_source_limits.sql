-- A page grant is still required. Keep the source restriction across page grant changes.
CREATE TABLE admin_message_source_limits (
    authorization_id UUID PRIMARY KEY REFERENCES authorizations(id) ON DELETE CASCADE,
    source_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT
);

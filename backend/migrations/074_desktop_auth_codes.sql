-- Short-lived, single-use PKCE grants transfer an existing session, never Lark credentials.
CREATE TABLE desktop_auth_codes (
    code_hash TEXT PRIMARY KEY CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    session_hash TEXT NOT NULL REFERENCES web_sessions(session_hash) ON DELETE CASCADE,
    session_ciphertext TEXT NOT NULL,
    code_challenge TEXT NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
    redirect_uri TEXT NOT NULL CHECK (char_length(redirect_uri) <= 256),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX desktop_auth_codes_expiry_idx ON desktop_auth_codes(expires_at);

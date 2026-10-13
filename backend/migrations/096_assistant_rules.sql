CREATE TABLE assistant_rule_overrides (
    rule_key TEXT PRIMARY KEY CHECK (rule_key IN ('core','understand','evidence','answer')),
    content_ciphertext TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    updated_by_authorization_id UUID NOT NULL REFERENCES authorizations(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

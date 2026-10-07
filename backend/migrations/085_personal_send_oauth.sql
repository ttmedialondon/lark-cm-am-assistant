-- Optional send authorization is bound to the already signed-in account.
-- NULL keeps pre-existing ordinary OAuth states compatible.
ALTER TABLE oauth_states
    ADD COLUMN requested_scopes TEXT[],
    ADD COLUMN authorization_id UUID REFERENCES authorizations(id) ON DELETE CASCADE;

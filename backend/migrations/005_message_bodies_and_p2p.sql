-- Keep the existing canonical message table. Historical index rows remain valid;
-- their body_ciphertext stays NULL until the authorising CM/AM reauthorises and
-- a safe full-history scan reads the message again.

ALTER TABLE authorizations
    ADD COLUMN body_consent_at TIMESTAMPTZ,
    ADD COLUMN p2p_discovery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (p2p_discovery_status IN ('pending', 'succeeded', 'unavailable', 'attention_required')),
    ADD COLUMN p2p_last_checked_at TIMESTAMPTZ,
    ADD COLUMN p2p_last_error TEXT;

ALTER TABLE chat_messages
    ADD COLUMN message_type TEXT,
    ADD COLUMN body_ciphertext TEXT,
    ADD COLUMN message_update_time TIMESTAMPTZ;

CREATE INDEX chat_messages_body_recent_idx
    ON chat_messages (message_create_time DESC, message_id DESC)
    WHERE NOT is_deleted AND body_ciphertext IS NOT NULL;

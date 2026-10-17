-- Every page verifies internal tenant evidence; cover that lookup without reading message bodies.
CREATE INDEX chat_messages_sender_tenant_idx
    ON chat_messages(sender_open_id, sender_tenant_key)
    WHERE sender_tenant_key IS NOT NULL;

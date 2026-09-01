-- Preserve the small amount of message structure needed to explain a future
-- creator-identity assessment. These are remote references, not foreign keys:
-- a parent/root message can be absent, deleted, or outside the authorised view.

ALTER TABLE chat_messages
    ADD COLUMN root_id TEXT,
    ADD COLUMN parent_id TEXT,
    ADD COLUMN thread_id TEXT,
    ADD COLUMN mentions_json JSONB,
    ADD COLUMN message_app_link TEXT,
    ADD COLUMN message_position BIGINT,
    ADD COLUMN thread_message_position BIGINT;

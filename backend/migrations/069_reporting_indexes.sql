-- Count observed messages by excluding the small deleted subset, without
-- joining every observation to a large encrypted message row.
CREATE INDEX chat_messages_deleted_lookup_idx ON chat_messages(chat_id, message_id) WHERE is_deleted;

-- Contact activity needs both live message counts and all observed chat IDs.
CREATE INDEX chat_messages_sender_activity_idx
    ON chat_messages(sender_open_id, chat_id) INCLUDE (is_deleted)
    WHERE sender_open_id IS NOT NULL;

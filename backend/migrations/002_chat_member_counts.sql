ALTER TABLE user_chats
    ADD COLUMN member_count INTEGER,
    ADD COLUMN member_count_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (member_count_status IN ('pending', 'succeeded', 'unavailable')),
    ADD COLUMN member_count_checked_at TIMESTAMPTZ,
    ADD CONSTRAINT user_chats_member_count_nonnegative
        CHECK (member_count IS NULL OR member_count >= 0);

CREATE INDEX user_chats_member_count_refresh_idx
    ON user_chats (authorization_id, member_count_checked_at);

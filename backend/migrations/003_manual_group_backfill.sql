ALTER TABLE user_chats
    ADD COLUMN manual_backfill_requested_at TIMESTAMPTZ,
    ADD COLUMN manual_backfill_started_at TIMESTAMPTZ,
    ADD COLUMN sync_generation INTEGER NOT NULL DEFAULT 0
        CHECK (sync_generation >= 0);

CREATE INDEX user_chats_manual_backfill_idx
    ON user_chats (authorization_id, manual_backfill_requested_at)
    WHERE manual_backfill_requested_at IS NOT NULL;

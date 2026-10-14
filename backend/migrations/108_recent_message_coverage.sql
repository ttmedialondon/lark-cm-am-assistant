-- A completed recent window is not proof that the account's full history has been read.
ALTER TABLE authorization_chats ADD COLUMN recent_coverage_start_at TIMESTAMPTZ;

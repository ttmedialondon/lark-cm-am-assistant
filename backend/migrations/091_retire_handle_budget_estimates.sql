-- Keep historical request accounting; new calls record actual usage, not obsolete budget estimates.
ALTER TABLE creator_handle_requests ALTER COLUMN quota_scope DROP NOT NULL;
ALTER TABLE creator_handle_requests ALTER COLUMN reserved_tokens DROP NOT NULL;
DROP INDEX creator_handle_request_budget;

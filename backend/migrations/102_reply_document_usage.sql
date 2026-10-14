-- Existing internal knowledge is not approved for outward-facing reply drafts.
ALTER TABLE assistant_rule_overrides ADD COLUMN reply_usable BOOLEAN NOT NULL DEFAULT FALSE;

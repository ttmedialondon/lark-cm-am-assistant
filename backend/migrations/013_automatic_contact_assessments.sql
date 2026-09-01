-- Automatic contact reviews use a message-arrival watermark, rather than the
-- mutable ``observed_at`` timestamp.  Re-reading an old Lark page must never
-- create another paid assessment.

ALTER TABLE authorization_message_observations
    ADD COLUMN first_observed_at TIMESTAMPTZ;

UPDATE authorization_message_observations
SET first_observed_at = observed_at
WHERE first_observed_at IS NULL;

ALTER TABLE authorization_message_observations
    ALTER COLUMN first_observed_at SET NOT NULL,
    ALTER COLUMN first_observed_at SET DEFAULT NOW();

CREATE INDEX authorization_message_observations_first_observed_idx
    ON authorization_message_observations (first_observed_at DESC);

ALTER TABLE contact_assessments
    ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'
        CHECK (trigger IN ('manual', 'automatic')),
    ADD COLUMN source_message_seen_at TIMESTAMPTZ;

CREATE INDEX contact_assessments_contact_source_seen_idx
    ON contact_assessments (contact_id, source_message_seen_at DESC, created_at DESC);

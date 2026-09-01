-- Keep the model result separate from the business decision.  A successful
-- assessment either writes only high-confidence facts, becomes a human review
-- candidate, or records that it produced no actionable recommendation.

ALTER TABLE ai_feature_configs
    ADD COLUMN auto_confirm_threshold NUMERIC(4, 3) NOT NULL DEFAULT 0.900
        CHECK (auto_confirm_threshold >= 0.500 AND auto_confirm_threshold <= 1.000);

ALTER TABLE contact_assessments
    ADD COLUMN outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending', 'auto_confirmed', 'review_required', 'no_action')),
    ADD COLUMN review_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (review_status IN ('not_required', 'pending', 'ignored')),
    ADD COLUMN reviewed_by_authorization_id UUID REFERENCES authorizations(id) ON DELETE SET NULL,
    ADD COLUMN reviewed_at TIMESTAMPTZ;

-- Existing successful shadow reviews remain available for the owner's manual
-- review instead of silently changing any fact during this migration.
UPDATE contact_assessments
SET outcome = CASE WHEN status = 'succeeded' THEN 'review_required' ELSE 'no_action' END,
    review_status = CASE WHEN status = 'succeeded' THEN 'pending' ELSE 'not_required' END
WHERE status NOT IN ('queued', 'running');

CREATE INDEX contact_assessments_review_queue_idx
    ON contact_assessments (review_status, created_at DESC)
    WHERE status = 'succeeded' AND outcome = 'review_required';

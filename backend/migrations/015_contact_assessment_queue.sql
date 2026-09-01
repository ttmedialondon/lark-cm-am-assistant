-- ``contact_assessments`` is the durable work queue.  Refreshing it at a
-- bounded cadence keeps newly eligible contacts flowing without re-scanning
-- every message on every worker loop.
ALTER TABLE ai_feature_configs
    ADD COLUMN automatic_queue_checked_at TIMESTAMPTZ NOT NULL
        DEFAULT '-infinity'::timestamptz;

-- Automatic facts are deliberately conservative: a lower configured value
-- must never confirm a contact without an owner review.
UPDATE ai_feature_configs
SET auto_confirm_threshold = 0.900
WHERE auto_confirm_threshold < 0.900;

ALTER TABLE ai_feature_configs
    DROP CONSTRAINT IF EXISTS ai_feature_configs_auto_confirm_threshold_check;

ALTER TABLE ai_feature_configs
    ADD CONSTRAINT ai_feature_configs_auto_confirm_threshold_check
        CHECK (auto_confirm_threshold >= 0.900 AND auto_confirm_threshold <= 1.000);

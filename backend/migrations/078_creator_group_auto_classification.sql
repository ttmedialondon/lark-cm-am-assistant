-- AI provenance is distinct from a human confirmation. Never replay past suggestions here.
ALTER TABLE chat_group_classifications
  ADD COLUMN source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','ai')),
  ADD COLUMN confidence NUMERIC(4,3) CHECK(confidence BETWEEN 0 AND 1),
  ALTER COLUMN confirmed_by_authorization_id DROP NOT NULL;
ALTER TABLE chat_group_classifications ADD CONSTRAINT chat_group_classification_provenance CHECK(
  (source='manual' AND confirmed_by_authorization_id IS NOT NULL AND confidence IS NULL)
  OR (source='ai' AND confirmed_by_authorization_id IS NULL AND source_job_id IS NOT NULL
      AND confidence IS NOT NULL AND purpose<>'unknown')
);
UPDATE ai_feature_configs SET auto_confirm_threshold=0.950
WHERE feature='creator_group_classification' AND auto_confirm_threshold=0.900;
CREATE INDEX creator_group_admission_retry ON creator_group_jobs(not_before,updated_at,id)
WHERE status='completed' AND stats->>'auto_deferred'='true';

-- Organization data is now maintained independently from role classification.
-- Wake only work that the former gate deferred; do not rewrite confirmed facts
-- or completed review history.
UPDATE contact_assessments
SET scheduled_at = NOW(), error_message = NULL, updated_at = NOW()
WHERE status = 'queued'
  AND error_message = '等待关联组织同步或人工确认组织。';

-- Run the durable eligibility scan immediately after deployment instead of
-- waiting for its ordinary one-minute refresh interval.
UPDATE ai_feature_configs
SET automatic_queue_checked_at = '-infinity'::timestamptz,
    updated_at = NOW()
WHERE feature = 'contact_assessment';

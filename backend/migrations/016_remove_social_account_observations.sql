-- Social-account appearances and ownership links are no longer part of
-- contact classification. Keep only role facts and message-backed evidence.

UPDATE contact_assessments
SET result = result - 'account_relations',
    updated_at = NOW()
WHERE result ? 'account_relations';

-- Rebuild unresolved automatic work under the role-only model instead of
-- letting an old successful watermark suppress a new assessment.
DELETE FROM contact_assessments assessment
WHERE assessment.trigger = 'automatic'
  AND NOT EXISTS (
      SELECT 1
      FROM contact_roles role
      WHERE role.contact_id = assessment.contact_id
        AND role.status = 'confirmed'
  );

DELETE FROM audit_events
WHERE action IN (
    'admin_social_account_viewed',
    'contact_assessment_account_relation_manually_confirmed'
);

UPDATE audit_events
SET metadata = metadata - 'account_relation_count'
WHERE action IN ('contact_assessment_auto_confirmed', 'contact_assessment_review_required')
  AND metadata ? 'account_relation_count';

UPDATE ai_feature_configs
SET automatic_queue_checked_at = '-infinity'::timestamptz,
    updated_at = NOW()
WHERE feature = 'contact_assessment';

DROP TABLE social_account_observations;
DROP TABLE contact_social_accounts;
DROP TABLE social_accounts;

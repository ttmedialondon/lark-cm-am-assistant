-- AI suggestions now require confirmation by the suggested CM/AM. Re-open
-- assignments written by the former automatic flow instead of presenting
-- them as if that person had already confirmed responsibility.
WITH ended_ai_assignments AS (
    UPDATE contact_assignments
    SET ended_at = NOW(), updated_at = NOW()
    WHERE source = 'ai' AND ended_at IS NULL
    RETURNING assessment_id
)
UPDATE contact_assignment_assessments assessment
SET outcome = 'review_required', updated_at = NOW()
FROM ended_ai_assignments ended
WHERE assessment.id = ended.assessment_id
  AND assessment.policy_version = 'direct_evidence_v2'
  AND assessment.status = 'succeeded'
  AND assessment.outcome = 'auto_assigned';

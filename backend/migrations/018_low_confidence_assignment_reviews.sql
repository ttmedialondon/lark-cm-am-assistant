-- A low-confidence candidate is not owner-review work. Keep the assessment
-- record and evidence, but make its current outcome match the 70% floor.
UPDATE contact_assignment_assessments
SET outcome = 'no_action', updated_at = NOW()
WHERE status = 'succeeded'
  AND outcome = 'review_required'
  AND COALESCE(
      CASE
          WHEN jsonb_typeof(result -> 'confidence') = 'number'
          THEN (result ->> 'confidence')::numeric
      END,
      0
  ) < 0.700;

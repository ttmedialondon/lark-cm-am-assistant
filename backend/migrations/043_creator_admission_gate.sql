-- Complete creator identity is mandatory; old person-only admission is retired.
ALTER TABLE creator_workflow_candidates
    ADD COLUMN handle_verification JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(handle_verification)='object'),
    ADD COLUMN ai_invalidated_at TIMESTAMPTZ;

-- A deployment after the explicit roster reset normally has no formal rows.
-- If old data exists, withdraw unsupported assignments rather than pretending
-- historical business_v2/CM clicks satisfy the new three-stage contract.
INSERT INTO audit_events(id,authorization_id,action,metadata)
VALUES(gen_random_uuid(),NULL,'creator_staged_admission_gate_enabled',jsonb_build_object(
    'withdrawn_legacy_formal',(SELECT count(*) FROM creator_workflow_candidates WHERE status='confirmed'),
    'source_people_messages_preserved',TRUE
));
UPDATE creator_account_assignments SET ended_at=NOW()
WHERE ended_at IS NULL AND candidate_id IN (
    SELECT id FROM creator_workflow_candidates WHERE status='confirmed'
);
UPDATE contact_social_accounts link SET status='rejected',updated_at=NOW()
WHERE starts_with(link.source,'creator_workflow_') AND EXISTS (
    SELECT 1 FROM creator_workflow_candidates candidate
    WHERE candidate.status='confirmed' AND candidate.contact_id=link.contact_id
      AND candidate.social_account_id=link.social_account_id
);
UPDATE creator_workflow_candidates SET status='pending',social_account_id=NULL,
    uncertainties=ARRAY['identity_ai_required','ownership_ai_required'],
    ai_invalidated_at=clock_timestamp(),version=version+1,updated_at=NOW()
WHERE status='confirmed';

ALTER TABLE creator_workflow_candidates ADD CONSTRAINT creator_complete_account_required
    CHECK (status<>'confirmed' OR (normalized_handle IS NOT NULL AND social_account_id IS NOT NULL));
ALTER TABLE creator_account_assignments ADD CONSTRAINT creator_active_assignment_account_required
    CHECK (ended_at IS NOT NULL OR social_account_id IS NOT NULL);

-- Reuse the contact queue for one whole-directory identity pass. Historical
-- results remain audit records; old queued work must never run under new rules.
ALTER TABLE contact_assessments
    ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'legacy';
CREATE INDEX contact_assessments_policy_contact_idx
    ON contact_assessments (policy_version, contact_id, created_at DESC);

ALTER TABLE contact_assessment_refs
    ADD COLUMN body_sha256 TEXT
        CHECK (body_sha256 IS NULL OR body_sha256 ~ '^[0-9a-f]{64}$');

UPDATE contact_assessments
SET status = 'cancelled', outcome = 'no_action', review_status = 'not_required',
    finished_at = NOW(), lease_until = NULL, updated_at = NOW(),
    error_message = '旧身份识别任务已停用；由新版全员身份识别重新处理。'
WHERE policy_version = 'legacy' AND status IN ('queued', 'running');

-- Deployment does not itself make paid requests. The requested whole-library
-- pass is explicitly enabled after application and authorization verification.
UPDATE ai_feature_configs
SET enabled = FALSE,
    auto_confirm_threshold = CASE WHEN feature = 'contact_assessment'
        THEN GREATEST(auto_confirm_threshold, 0.950) ELSE auto_confirm_threshold END,
    automatic_queue_checked_at = '-infinity'::timestamptz, updated_at = NOW()
WHERE feature IN ('contact_assessment', 'contact_assignment', 'creator_workflow');

INSERT INTO audit_events(id, authorization_id, action, metadata)
VALUES (gen_random_uuid(), NULL, 'people_identity_policy_installed',
    jsonb_build_object('policy_version', 'people-identity-v1',
        'contacts_messages_assignments_preserved', TRUE,
        'paid_scan_started', FALSE));

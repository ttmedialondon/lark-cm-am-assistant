-- Re-run owner matching under the direct-evidence policy without discarding
-- historical assessments or any manually confirmed business relationship.
ALTER TABLE contact_assignment_assessments
    ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'legacy'
        CHECK (length(btrim(policy_version)) BETWEEN 1 AND 80);

CREATE INDEX contact_assignment_assessments_policy_contact_source_idx
    ON contact_assignment_assessments (policy_version, contact_id, source_message_seen_at DESC, created_at DESC);

-- No legacy work may run after the evidence rules change. Keep its audit trail.
UPDATE contact_assignment_assessments
SET status = 'cancelled',
    outcome = 'no_action',
    finished_at = NOW(),
    lease_until = NULL,
    error_message = '负责人匹配规则已更新；将按新的直接互动规则重新扫描。',
    updated_at = NOW()
WHERE status IN ('queued', 'running');

-- A legacy AI inference is not ground truth. Preserve manual owners unchanged.
UPDATE contact_assignments
SET ended_at = NOW(),
    updated_at = NOW()
WHERE source = 'ai' AND ended_at IS NULL;

-- Start the new policy from the full permitted recent window.
UPDATE ai_feature_configs
SET automatic_queue_checked_at = '-infinity'::timestamptz,
    updated_at = NOW()
WHERE feature = 'contact_assignment';

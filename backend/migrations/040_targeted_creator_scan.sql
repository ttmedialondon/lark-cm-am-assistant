-- Retire whole-chat jobs without deleting historical outputs or source messages.
ALTER TABLE creator_workflow_ai_runs
    ADD COLUMN scan_mode TEXT NOT NULL DEFAULT 'legacy' CHECK (scan_mode IN ('legacy','person_v1')),
    ADD COLUMN preview_only BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN consent_at TIMESTAMPTZ,
    ADD COLUMN preflight_json JSONB NOT NULL DEFAULT '{}';
ALTER TABLE creator_workflow_ai_runs DROP CONSTRAINT creator_workflow_ai_runs_status_check;
ALTER TABLE creator_workflow_ai_runs ADD CONSTRAINT creator_workflow_ai_runs_status_check
    CHECK (status IN ('ready','queued','running','succeeded','failed','cancelled'));
ALTER TABLE creator_workflow_ai_tasks
    ADD COLUMN contact_id UUID REFERENCES contacts(id),
    ADD COLUMN evidence_refs JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN person_metadata JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN person_outcome TEXT CHECK (person_outcome IN ('review','uncertain','no_candidate'));
CREATE INDEX creator_workflow_ai_tasks_person_fingerprint_idx
    ON creator_workflow_ai_tasks(contact_id,input_fingerprint);
UPDATE creator_workflow_ai_tasks SET status='cancelled',lease_until=NULL,finished_at=NOW(),
    error_message='旧版整群扫描已停用；请使用按人预检。' WHERE status IN ('queued','running');
UPDATE creator_workflow_ai_runs SET status='cancelled',finished_at=NOW()
    WHERE status IN ('queued','running');
DROP INDEX creator_workflow_ai_runs_active_cm_idx;
CREATE UNIQUE INDEX creator_workflow_ai_runs_active_cm_idx
    ON creator_workflow_ai_runs(responsible_authorization_id)
    WHERE status IN ('ready','queued','running');
ALTER TABLE creator_workflow_onboarding DROP CONSTRAINT creator_workflow_onboarding_status_check;
ALTER TABLE creator_workflow_onboarding ADD CONSTRAINT creator_workflow_onboarding_status_check
    CHECK (status IN ('waiting_sync','ready','scanning','succeeded','failed','cancelled'));
UPDATE creator_workflow_onboarding SET status='cancelled',next_check_at=NULL,finished_at=NOW(),
    error_message='旧版整群扫描已停用；请使用按人预检。' WHERE status='scanning';

-- Keep the existing durable queue; a task now represents a fixed message segment.
ALTER TABLE creator_workflow_ai_runs DROP CONSTRAINT creator_workflow_ai_runs_history_days_check;
ALTER TABLE creator_workflow_ai_runs ADD CONSTRAINT creator_workflow_ai_runs_history_days_check
    CHECK (history_days BETWEEN 0 AND 90); -- 0 means all currently authorized, synced history.
ALTER TABLE creator_workflow_ai_tasks ADD COLUMN message_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE creator_workflow_ai_tasks DROP CONSTRAINT creator_workflow_ai_tasks_run_id_chat_id_key;
ALTER TABLE creator_workflow_ai_tasks ADD UNIQUE(run_id,chat_id,input_fingerprint);
ALTER TABLE creator_workflow_onboarding ADD COLUMN requested_by_authorization_id UUID
    REFERENCES authorizations(id);

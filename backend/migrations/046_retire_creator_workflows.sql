-- Retire only the old ownership/intake execution paths. Keep every business row,
-- historical result, credential, message and current people-identity task.
UPDATE ai_feature_configs SET enabled=FALSE,updated_at=NOW()
    WHERE feature IN ('contact_assignment','creator_workflow') AND enabled;

UPDATE contact_assignment_assessments
    SET status='cancelled',lease_until=NULL,finished_at=NOW(),updated_at=NOW(),
        error_message='旧负责人归属工程已移除，等待新规则。'
    WHERE status IN ('queued','running');
UPDATE creator_workflow_ai_tasks
    SET status='cancelled',lease_until=NULL,finished_at=NOW(),
        error_message='旧三阶段工程已移除，等待新规则。'
    WHERE status IN ('queued','running');
UPDATE creator_workflow_ai_runs SET status='cancelled',finished_at=NOW(),continuous=FALSE
    WHERE status IN ('ready','queued','running');
UPDATE creator_workflow_onboarding
    SET status='cancelled',next_check_at=NULL,finished_at=NOW(),
        error_message='旧自动接入扫描已移除，等待新规则。'
    WHERE status IN ('waiting_sync','ready','scanning');

INSERT INTO audit_events(id,authorization_id,action,metadata)
SELECT gen_random_uuid(),NULL,'creator_workflows_retired_20260911',
    '{"business_data_preserved":true,"identity_classification_unchanged":true,"replacement_started":false}'::jsonb
WHERE NOT EXISTS (SELECT 1 FROM audit_events WHERE action='creator_workflows_retired_20260911');

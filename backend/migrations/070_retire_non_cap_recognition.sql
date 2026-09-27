-- Retire execution only: keep confirmed identities, assignments, CAP imports,
-- messages, credentials and historical evidence unchanged.
-- CAP previously shared the identity feature's key; copy its encrypted config
-- into a dedicated namespace without decrypting or rotating the key.
INSERT INTO ai_feature_configs(feature,provider,project_id,model,enabled,api_key_ciphertext,
    key_suffix,key_updated_at,key_updated_by_authorization_id,last_tested_at,last_test_status,
    auto_confirm_threshold)
SELECT 'creator_import',provider,project_id,model,FALSE,api_key_ciphertext,key_suffix,
    key_updated_at,key_updated_by_authorization_id,last_tested_at,last_test_status,auto_confirm_threshold
FROM ai_feature_configs WHERE feature='contact_assessment'
ON CONFLICT(feature) DO NOTHING;

UPDATE ai_feature_configs SET enabled=FALSE,updated_at=NOW()
WHERE feature IN ('contact_assessment','contact_assignment','creator_workflow') AND enabled;

UPDATE contact_assessments SET status='cancelled',lease_until=NULL,finished_at=NOW(),
    updated_at=NOW(),error_message='旧身份识别机制已退役。'
WHERE status IN ('queued','running');
UPDATE contact_assignment_assessments SET status='cancelled',lease_until=NULL,finished_at=NOW(),
    updated_at=NOW(),error_message='旧负责人识别机制已退役。'
WHERE status IN ('queued','running');
UPDATE creator_ownership_tasks SET status='cancelled',reason_code='retired',lease_until=NULL,
    finished_at=NOW(),updated_at=NOW()
WHERE status IN ('pending','preparing','ready','queued','running');
UPDATE creator_ownership_runs SET status='cancelled',updated_at=NOW()
WHERE status IN ('preparing','preview','running','paused');
UPDATE creator_handle_tasks SET status='invalid',reason_code='retired',lease_until=NULL,
    finished_at=NOW(),updated_at=NOW()
WHERE status IN ('pending','preparing','ready','queued','running');
UPDATE creator_handle_runs SET status='completed',auto_admit=FALSE,finished_at=NOW(),updated_at=NOW()
WHERE status IN ('preparing','preview','running','paused') OR auto_admit;
UPDATE creator_workflow_ai_tasks SET status='cancelled',lease_until=NULL,finished_at=NOW(),
    error_message='旧三阶段识别机制已退役。' WHERE status IN ('queued','running');
UPDATE creator_workflow_ai_runs SET status='cancelled',continuous=FALSE,finished_at=NOW()
WHERE status IN ('ready','queued','running') OR continuous;
UPDATE creator_workflow_onboarding SET status='cancelled',next_check_at=NULL,finished_at=NOW(),
    error_message='旧自动接入扫描已退役。' WHERE status IN ('waiting_sync','ready','scanning');

INSERT INTO audit_events(id,action,metadata)
SELECT gen_random_uuid(),'non_cap_recognition_retired_20260927',
    '{"cap_imports_preserved":true,"business_data_preserved":true,"history_preserved":true}'::jsonb
WHERE NOT EXISTS (SELECT 1 FROM audit_events WHERE action='non_cap_recognition_retired_20260927');

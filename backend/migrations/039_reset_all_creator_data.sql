-- Explicit owner request (2026-09-10): restart every CM's creator roster.
-- Deployment stops workers and verifies a database backup before this migration.
-- Source people, Lark identities, chats, observations, authorizations and grants stay intact.
LOCK TABLE creator_workflow_candidates,creator_workflow_sources,creator_account_assignments,
    contact_social_accounts,social_accounts,creator_workflow_requests,creator_workflow_import_batches,
    creator_workflow_ai_runs,creator_workflow_ai_tasks,creator_workflow_onboarding,
    creator_workflow_ai_chat_checks,offhours_summary_settings,offhours_summary_runs,
    offhours_summary_tasks,contact_assignments,contact_assignment_assessments,
    contact_assessments,contact_roles IN ACCESS EXCLUSIVE MODE;

INSERT INTO audit_events(id,authorization_id,action,metadata)
SELECT gen_random_uuid(),NULL,'all_cm_creator_data_reset',jsonb_build_object(
    'scope','all_cm_creator_derived_data',
    'candidates',(SELECT count(*) FROM creator_workflow_candidates),
    'sources',(SELECT count(*) FROM creator_workflow_sources),
    'assignments',(SELECT count(*) FROM creator_account_assignments),
    'accounts',(SELECT count(*) FROM social_accounts),
    'ai_runs',(SELECT count(*) FROM creator_workflow_ai_runs),
    'ai_tasks',(SELECT count(*) FROM creator_workflow_ai_tasks),
    'summary_tasks',(SELECT count(*) FROM offhours_summary_tasks),
    'protected_counts',jsonb_build_object(
        'contacts',(SELECT count(*) FROM contacts),
        'lark_identities',(SELECT count(*) FROM lark_identities),
        'chat_messages',(SELECT count(*) FROM chat_messages),
        'observations',(SELECT count(*) FROM authorization_message_observations),
        'authorizations',(SELECT count(*) FROM authorizations),
        'admin_roles',(SELECT count(*) FROM admin_roles),
        'page_grants',(SELECT count(*) FROM admin_page_permissions)
    ),
    'source_people_messages_authorizations_preserved',TRUE
);

UPDATE ai_feature_configs SET enabled=FALSE,updated_at=NOW()
WHERE feature IN ('creator_workflow','contact_assessment','contact_assignment');

-- Remove derived reports/queues too, so they cannot display or re-create the old roster.
DELETE FROM offhours_summary_settings;
DELETE FROM offhours_summary_tasks;
DELETE FROM offhours_summary_runs;
DELETE FROM creator_workflow_ai_tasks;
UPDATE creator_workflow_onboarding SET run_id=NULL;
DELETE FROM creator_workflow_ai_runs;
DELETE FROM creator_workflow_onboarding;
DELETE FROM creator_workflow_ai_chat_checks;
DELETE FROM creator_workflow_import_batches;
DELETE FROM creator_workflow_requests;
DELETE FROM creator_workflow_sources;
DELETE FROM creator_account_assignments;
DELETE FROM contact_social_accounts;
DELETE FROM creator_workflow_candidates;
DELETE FROM social_accounts;

-- Retire remaining legacy creator facts, not other contact roles or identity evidence.
DELETE FROM contact_assignments;
DELETE FROM contact_assignment_assessments;
DELETE FROM contact_assessments
WHERE result->'roles' @> '[{"role":"creator"}]'::jsonb;
UPDATE contact_assessments SET status='cancelled',finished_at=NOW(),lease_until=NULL,
    error_message='达人规则已重置，旧身份任务不再执行。',updated_at=NOW()
WHERE status IN ('queued','running');
DELETE FROM contact_roles WHERE role='creator';

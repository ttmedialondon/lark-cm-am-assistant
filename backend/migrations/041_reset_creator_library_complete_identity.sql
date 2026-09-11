-- Owner request (2026-09-11): empty the creator library before redesigning admission.
-- Stop web/worker and verify a database backup first. This is not a database reset.
DO $$
DECLARE
    removed JSONB;
    protected_counts JSONB;
    account_count BIGINT;
BEGIN
    -- Never erase a future roster if this one-off migration is accidentally replayed.
    IF EXISTS (SELECT 1 FROM audit_events
               WHERE action='creator_library_reset_complete_identity_20260911') THEN
        RETURN;
    END IF;

    LOCK TABLE creator_workflow_candidates,creator_workflow_sources,creator_account_assignments,
        contact_social_accounts,social_accounts,creator_workflow_requests,creator_workflow_import_batches,
        creator_workflow_ai_runs,creator_workflow_ai_tasks,creator_workflow_onboarding,
        creator_workflow_ai_chat_checks,offhours_summary_settings,offhours_summary_runs,
        offhours_summary_tasks,contact_assignments,contact_assignment_assessments,
        contact_assessments,contact_roles,ai_feature_configs IN ACCESS EXCLUSIVE MODE;

    CREATE TEMP TABLE creator_reset_people ON COMMIT DROP AS
        SELECT contact_id FROM creator_workflow_candidates WHERE contact_id IS NOT NULL
        UNION SELECT contact_id FROM creator_account_assignments
        UNION SELECT contact_id FROM contact_roles WHERE role='creator'
        UNION SELECT contact_id FROM contact_social_accounts WHERE starts_with(source,'creator_workflow_')
        UNION SELECT contact_id FROM contact_assessments WHERE result->'roles' @> '[{"role":"creator"}]'::jsonb;
    CREATE TEMP TABLE creator_reset_links ON COMMIT DROP AS
        SELECT link.contact_id,link.social_account_id,link.relationship FROM contact_social_accounts link
        WHERE starts_with(link.source,'creator_workflow_')
           OR EXISTS (SELECT 1 FROM creator_workflow_candidates candidate
                      WHERE candidate.contact_id=link.contact_id AND candidate.social_account_id=link.social_account_id)
           OR EXISTS (SELECT 1 FROM creator_account_assignments assignment
                      WHERE assignment.contact_id=link.contact_id AND assignment.social_account_id=link.social_account_id);
    CREATE TEMP TABLE creator_reset_accounts ON COMMIT DROP AS
        SELECT social_account_id FROM creator_reset_links
        UNION SELECT social_account_id FROM creator_workflow_candidates WHERE social_account_id IS NOT NULL
        UNION SELECT social_account_id FROM creator_account_assignments WHERE social_account_id IS NOT NULL;

    SELECT count(*) INTO account_count FROM social_accounts;
    SELECT jsonb_build_object(
        'candidates',(SELECT count(*) FROM creator_workflow_candidates),
        'sources',(SELECT count(*) FROM creator_workflow_sources),
        'assignments',(SELECT count(*) FROM creator_account_assignments),
        'social_links',(SELECT count(*) FROM creator_reset_links),
        'ai_runs',(SELECT count(*) FROM creator_workflow_ai_runs),
        'ai_tasks',(SELECT count(*) FROM creator_workflow_ai_tasks),
        'onboarding',(SELECT count(*) FROM creator_workflow_onboarding),
        'chat_checks',(SELECT count(*) FROM creator_workflow_ai_chat_checks),
        'import_batches',(SELECT count(*) FROM creator_workflow_import_batches),
        'requests',(SELECT count(*) FROM creator_workflow_requests),
        'summary_settings',(SELECT count(*) FROM offhours_summary_settings),
        'summary_runs',(SELECT count(*) FROM offhours_summary_runs),
        'summary_tasks',(SELECT count(*) FROM offhours_summary_tasks),
        'legacy_assignments',(SELECT count(*) FROM contact_assignments WHERE contact_id IN (SELECT contact_id FROM creator_reset_people)),
        'legacy_assignment_assessments',(SELECT count(*) FROM contact_assignment_assessments WHERE contact_id IN (SELECT contact_id FROM creator_reset_people)),
        'creator_assessments',(SELECT count(*) FROM contact_assessments WHERE result->'roles' @> '[{"role":"creator"}]'::jsonb),
        'creator_roles',(SELECT count(*) FROM contact_roles WHERE role='creator')
    ) INTO removed;
    SELECT jsonb_build_object(
        'contacts',(SELECT count(*) FROM contacts),
        'lark_identities',(SELECT count(*) FROM lark_identities),
        'chats',(SELECT count(*) FROM chats),
        'chat_messages',(SELECT count(*) FROM chat_messages),
        'observations',(SELECT count(*) FROM authorization_message_observations),
        'authorizations',(SELECT count(*) FROM authorizations),
        'authorization_chats',(SELECT count(*) FROM authorization_chats),
        'admin_roles',(SELECT count(*) FROM admin_roles),
        'page_grants',(SELECT count(*) FROM admin_page_permissions),
        'non_creator_roles',(SELECT count(*) FROM contact_roles WHERE role<>'creator')
    ) INTO protected_counts;

    UPDATE ai_feature_configs SET enabled=FALSE,updated_at=NOW()
        WHERE feature IN ('creator_workflow','contact_assessment','contact_assignment','offhours_summary');
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
    DELETE FROM contact_social_accounts link USING creator_reset_links target
        WHERE (link.contact_id,link.social_account_id,link.relationship)
            =(target.contact_id,target.social_account_id,target.relationship);
    DELETE FROM creator_workflow_candidates;
    -- Keep an account if a separate, unrelated person/account link still needs it.
    DELETE FROM social_accounts account USING creator_reset_accounts target
        WHERE account.id=target.social_account_id
          AND NOT EXISTS (SELECT 1 FROM contact_social_accounts link WHERE link.social_account_id=account.id);
    DELETE FROM contact_assignments WHERE contact_id IN (SELECT contact_id FROM creator_reset_people);
    DELETE FROM contact_assignment_assessments WHERE contact_id IN (SELECT contact_id FROM creator_reset_people);
    DELETE FROM contact_assessments WHERE result->'roles' @> '[{"role":"creator"}]'::jsonb;
    DELETE FROM contact_roles WHERE role='creator';

    INSERT INTO audit_events(id,authorization_id,action,metadata)
    VALUES(gen_random_uuid(),NULL,'creator_library_reset_complete_identity_20260911',jsonb_build_object(
        'scope','all_cm_creator_derived_data',
        'deleted',removed||jsonb_build_object('social_accounts',account_count-(SELECT count(*) FROM social_accounts)),
        'protected_counts',protected_counts,
        'source_people_messages_authorizations_preserved',TRUE,
        'ai_features_disabled',jsonb_build_array('creator_workflow','contact_assessment','contact_assignment','offhours_summary'),
        'model_and_key_configuration_preserved',TRUE
    ));
    DROP TABLE creator_reset_accounts,creator_reset_links,creator_reset_people;
END $$;

-- Owner request, 2026-09-14: reset every CM's creator library and creator/CAP
-- classifications before defining new rules. Deployment stops web/worker and
-- verifies a database backup first. Keep source data and paid-analysis history.
DO $$
DECLARE
    saved JSONB;
BEGIN
    IF EXISTS (SELECT 1 FROM audit_events
               WHERE action='creator_library_reset_for_new_rules_20260914') THEN
        RETURN;
    END IF;

    LOCK TABLE contact_roles,contact_assignments,creator_account_assignments,
        contact_social_accounts,contact_assessments,contact_assignment_assessments,
        creator_ownership_runs,creator_ownership_tasks,creator_handle_runs,creator_handle_tasks,
        creator_import_batches,creator_import_rows,creator_workflow_candidates,
        creator_workflow_ai_runs,creator_workflow_ai_tasks,creator_workflow_onboarding,
        ai_feature_configs IN ACCESS EXCLUSIVE MODE;

    CREATE TEMP TABLE reset_creator_people ON COMMIT DROP AS
        SELECT contact_id FROM contact_roles WHERE role='creator' AND status='confirmed';
    SELECT jsonb_build_object(
        'scope','all_cm_creator_library_and_creator_classification',
        'ended_person_assignments',(SELECT count(*) FROM contact_assignments WHERE ended_at IS NULL),
        'ended_account_assignments',(SELECT count(*) FROM creator_account_assignments WHERE ended_at IS NULL),
        'removed_creator_roles',COALESCE((SELECT jsonb_agg(to_jsonb(r)) FROM contact_roles r
            WHERE role='creator' AND status='confirmed'),'[]'),
        'removed_account_links',COALESCE((SELECT jsonb_agg(to_jsonb(l)) FROM contact_social_accounts l),'[]'),
        'original_creator_types',COALESCE((SELECT jsonb_agg(jsonb_build_object('contact_id',id,'creator_type',creator_type))
            FROM contacts WHERE creator_type<>'pending'),'[]'),
        'import_batches',(SELECT count(*) FROM creator_import_batches),
        'import_rows',(SELECT count(*) FROM creator_import_rows),
        'ai_features',COALESCE((SELECT jsonb_agg(jsonb_build_object('feature',feature,'enabled',enabled))
            FROM ai_feature_configs WHERE feature IN ('contact_assessment','contact_assignment','creator_workflow')),'[]'),
        'protected_counts',jsonb_build_object(
            'contacts',(SELECT count(*) FROM contacts),
            'lark_identities',(SELECT count(*) FROM lark_identities),
            'chats',(SELECT count(*) FROM chats),
            'chat_messages',(SELECT count(*) FROM chat_messages),
            'observations',(SELECT count(*) FROM authorization_message_observations),
            'authorizations',(SELECT count(*) FROM authorizations),
            'non_creator_roles',(SELECT count(*) FROM contact_roles WHERE role<>'creator')),
        'source_data_and_analysis_history_preserved',TRUE,
        'new_scan_started',FALSE
    ) INTO saved;

    UPDATE ai_feature_configs SET enabled=FALSE,updated_at=NOW()
        WHERE feature IN ('contact_assessment','contact_assignment','creator_workflow');
    UPDATE contact_assignments SET ended_at=NOW(),updated_at=NOW() WHERE ended_at IS NULL;
    UPDATE creator_account_assignments SET ended_at=NOW() WHERE ended_at IS NULL;
    -- Rejected links would block a new match too. Retain their complete rows in
    -- the reset audit instead of leaving them as effective account constraints.
    DELETE FROM contact_social_accounts;
    DELETE FROM contact_roles WHERE role='creator' AND status='confirmed';
    UPDATE contacts SET creator_type='pending',updated_at=NOW() WHERE creator_type<>'pending';

    -- Keep identity results/usage, explicit non-creator decisions and rejected
    -- creator exclusions. Archive the old creator conclusions for a fresh scan.
    UPDATE contact_assessments SET policy_version=policy_version||':reset-20260914',updated_at=NOW()
        WHERE (contact_id IN (SELECT contact_id FROM reset_creator_people)
               OR result->'roles' @> '[{"role":"creator"}]'::jsonb)
          AND NOT COALESCE(status='succeeded' AND result->>'reason_code'='classified'
              AND result->'roles'->0->>'role'<>'creator',FALSE);
    UPDATE contact_assessments SET status='cancelled',outcome='no_action',review_status='not_required',
        finished_at=NOW(),lease_until=NULL,updated_at=NOW(),error_message='达人库已重置，等待新规则。'
        WHERE status IN ('queued','running');
    UPDATE contact_assignment_assessments SET policy_version=policy_version||':reset-20260914',
        status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,
        lease_until=NULL,updated_at=NOW();

    -- Completed ownership runs can seed handles, and completed handle runs can
    -- auto-admit. Archive both parents and tasks, not just currently running jobs.
    UPDATE creator_ownership_runs SET policy_version=policy_version||':reset-20260914',
        status='cancelled',updated_at=NOW();
    UPDATE creator_ownership_tasks SET policy_version=policy_version||':reset-20260914',
        status=CASE WHEN status IN ('pending','preparing','ready','queued','running') THEN 'cancelled' ELSE status END,
        lease_until=NULL,updated_at=NOW();
    UPDATE creator_handle_runs SET policy_version=policy_version||':reset-20260914',
        auto_admit=FALSE,status='completed',updated_at=NOW();
    UPDATE creator_handle_tasks SET policy_version=policy_version||':reset-20260914',
        reason_code=CASE WHEN status IN ('pending','preparing','ready','queued','running') THEN 'library_reset' ELSE reason_code END,
        status=CASE WHEN status IN ('pending','preparing','ready','queued','running') THEN 'invalid' ELSE status END,
        lease_until=NULL,updated_at=NOW();

    -- Preserve the original roster and paid results. The import cache does not
    -- filter by batch policy, so explicitly invalidate its row fingerprint too.
    UPDATE creator_import_batches SET policy_version=policy_version||':reset-20260914',
        status='completed',updated_at=NOW();
    UPDATE creator_import_rows SET
        stats=stats||jsonb_build_object('archived_fingerprint',fingerprint,'archived_for_rule_reset',TRUE),
        fingerprint=NULL,
        reason_code=CASE WHEN status IN ('pending','preparing','queued','running') THEN 'library_reset' ELSE reason_code END,
        status=CASE WHEN status IN ('pending','preparing','queued','running') THEN 'invalid' ELSE status END,
        lease_until=NULL,updated_at=NOW();

    -- Retired workflows remain audit history, never an executable queue.
    UPDATE creator_workflow_ai_runs SET continuous=FALSE,
        status=CASE WHEN status IN ('ready','queued','running') THEN 'cancelled' ELSE status END;
    UPDATE creator_workflow_ai_tasks SET lease_until=NULL,
        status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END;
    UPDATE creator_workflow_onboarding SET status='cancelled',next_check_at=NULL,finished_at=NOW(),
        error_message='达人库已重置，等待新规则。' WHERE status IN ('waiting_sync','ready','scanning');
    -- Keep retired candidate history unchanged: confirmed and pending rows for
    -- the same handle may coexist, and changing status can violate uniqueness.

    INSERT INTO audit_events(id,authorization_id,action,metadata)
        VALUES(gen_random_uuid(),NULL,'creator_library_reset_for_new_rules_20260914',saved);
    DROP TABLE reset_creator_people;
END $$;

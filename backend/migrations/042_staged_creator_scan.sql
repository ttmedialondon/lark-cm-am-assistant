-- Sequential person assessment; no feature is enabled and no task is enqueued.
ALTER TABLE creator_workflow_ai_runs DROP CONSTRAINT creator_workflow_ai_runs_scan_mode_check;
ALTER TABLE creator_workflow_ai_runs ADD CONSTRAINT creator_workflow_ai_runs_scan_mode_check
    CHECK (scan_mode IN ('legacy','person_v1','person_staged_v1'));
ALTER TABLE creator_workflow_ai_runs ADD COLUMN started_at TIMESTAMPTZ;
ALTER TABLE creator_workflow_ai_tasks
    ADD COLUMN stage TEXT NOT NULL DEFAULT 'identity' CHECK (stage IN ('identity','ownership','handle')),
    ADD COLUMN stage_results JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(stage_results)='object'),
    ADD COLUMN initial_fingerprint TEXT,
    ADD COLUMN stage_attempts INTEGER NOT NULL DEFAULT 0 CHECK (stage_attempts BETWEEN 0 AND 2),
    ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 1);
UPDATE creator_workflow_ai_tasks SET initial_fingerprint=input_fingerprint;
ALTER TABLE creator_workflow_ai_tasks ALTER COLUMN initial_fingerprint SET NOT NULL;
CREATE INDEX creator_workflow_ai_tasks_initial_fingerprint_idx
    ON creator_workflow_ai_tasks(contact_id,initial_fingerprint);
ALTER TABLE creator_workflow_ai_tasks DROP CONSTRAINT creator_workflow_ai_tasks_person_outcome_check;
ALTER TABLE creator_workflow_ai_tasks ADD CONSTRAINT creator_workflow_ai_tasks_person_outcome_check
    CHECK (person_outcome IN ('review','uncertain','no_candidate','not_creator','not_mine',
        'identity_unknown','ownership_unknown','handle_missing','handle_conflict','confirmed'));
UPDATE creator_workflow_ai_tasks t SET status='cancelled',lease_until=NULL,finished_at=NOW(),
    error_message='旧判断流程已停用，请重新准备三阶段核对。'
    FROM creator_workflow_ai_runs r WHERE t.run_id=r.id AND r.scan_mode<>'person_staged_v1'
      AND t.status IN ('queued','running');
UPDATE creator_workflow_ai_runs SET status='cancelled',finished_at=NOW()
    WHERE scan_mode<>'person_staged_v1' AND status IN ('ready','queued','running');

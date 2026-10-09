-- Rebuild only started London periods in the visible seven-day range. Preserve
-- old results, checkpoints and billing; invalidate old leases before new work.
WITH affected_runs AS MATERIALIZED (
    SELECT r.id FROM offhours_summary_runs r
    JOIN authorizations a ON a.id=r.responsible_authorization_id
    WHERE r.period IS NOT NULL AND r.window_start<=NOW()
      AND r.report_date BETWEEN (NOW() AT TIME ZONE 'Europe/London')::date-6
                            AND (NOW() AT TIME ZONE 'Europe/London')::date
      AND a.status='active' AND a.body_consent_at IS NOT NULL
      AND r.coverage->>'policy' IS DISTINCT FROM 'shift-handling-v5'
), retired AS (
    UPDATE offhours_summary_tasks t SET is_current=FALSE,
        status=CASE WHEN t.status IN ('queued','running') THEN 'cancelled' ELSE t.status END,
        error_message=CASE WHEN t.status IN ('queued','running')
            THEN '总结规则已更新，等待新版任务。' ELSE t.error_message END,
        finished_at=CASE WHEN t.status IN ('queued','running') THEN NOW() ELSE t.finished_at END,
        lease_until=NULL
    FROM affected_runs r WHERE t.run_id=r.id AND t.is_current
    RETURNING t.id
), refreshed AS (
    UPDATE offhours_summary_runs r SET next_check_at=NOW(),
        refresh_until=GREATEST(COALESCE(r.refresh_until,r.window_end),NOW()+INTERVAL '1 day'),
        updated_at=NOW()
    FROM affected_runs a WHERE r.id=a.id
    RETURNING r.id
)
INSERT INTO audit_events(id,action,metadata)
SELECT gen_random_uuid(),'summary_v5_replay_requested',
    jsonb_build_object('policy','shift-handling-v5','days',7,
        'runs',(SELECT count(*) FROM refreshed),'retired_tasks',(SELECT count(*) FROM retired));

-- Existing scheduling fills missing started periods, without enabling a feature.
UPDATE offhours_summary_settings SET next_period_check_at=NULL WHERE id AND recurring_enabled;

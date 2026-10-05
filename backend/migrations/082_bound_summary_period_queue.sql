-- Preserve reports, checkpoints, usage and business facts. Never execute a stale
-- period outside the visible London seven-day range, including after a restart.
WITH stopped AS (
    UPDATE offhours_summary_tasks t SET status='cancelled',lease_until=NULL,finished_at=NOW(),
        error_message='超出最近七天范围，未继续处理。'
    FROM offhours_summary_runs r
    WHERE r.id=t.run_id AND r.period IS NOT NULL
      AND r.report_date < (NOW() AT TIME ZONE 'Europe/London')::date-6
      AND t.status IN ('queued','running')
    RETURNING t.id
)
INSERT INTO audit_events(id,action,metadata)
SELECT gen_random_uuid(),'summary_out_of_range_tasks_stopped',jsonb_build_object('tasks',count(*))
FROM stopped HAVING count(*)>0;

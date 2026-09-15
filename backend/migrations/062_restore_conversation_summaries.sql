-- Keep historical timeline results, but retire their incompatible queue entries.
UPDATE offhours_summary_tasks SET is_current=FALSE,
    status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,
    lease_until=NULL
    WHERE contact_id IS NOT NULL;
-- Re-run every conversation in the visible seven-day range using the simple workflow.
-- Existing encrypted results remain until a successful replacement is saved.
UPDATE offhours_summary_tasks t SET is_current=FALSE,
    status='cancelled',attempts=0,lease_until=NULL,scheduled_at=NOW(),error_message=NULL
    FROM offhours_summary_runs r WHERE r.id=t.run_id AND t.chat_id IS NOT NULL
    AND r.period IS NOT NULL AND r.report_date >= (NOW() AT TIME ZONE 'Europe/London')::date-6;
UPDATE offhours_summary_runs SET coverage='{}',next_check_at=NOW(),
    refresh_until=GREATEST(refresh_until,NOW()+INTERVAL '1 day')
    WHERE period IS NOT NULL AND report_date >= (NOW() AT TIME ZONE 'Europe/London')::date-6;

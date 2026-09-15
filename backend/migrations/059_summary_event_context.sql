-- Replace unfinished broad history baselines with event-directed history lookup.
UPDATE offhours_summary_tasks t SET is_current=FALSE,
    status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,lease_until=NULL
    FROM offhours_summary_runs r WHERE r.id=t.run_id AND t.contact_id IS NOT NULL AND r.period IS NOT NULL;
UPDATE offhours_summary_runs SET next_check_at=NOW(),refresh_until=GREATEST(refresh_until,NOW()+INTERVAL '1 day'),
    coverage='{}' WHERE period IS NOT NULL
    AND report_date >= (NOW() AT TIME ZONE 'Europe/London')::date-6;

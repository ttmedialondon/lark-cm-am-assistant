-- Keep prior reports for audit; the new result format cannot reuse old checkpoints.
UPDATE offhours_summary_tasks SET is_current=FALSE,
    status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,lease_until=NULL;
UPDATE offhours_summary_runs SET coverage='{}',next_check_at=NOW(),
    refresh_until=GREATEST(refresh_until,NOW()+INTERVAL '1 day')
    WHERE period IS NOT NULL AND report_date >= (NOW() AT TIME ZONE 'Europe/London')::date-6;
UPDATE offhours_summary_settings SET next_check_at=NOW(),next_period_check_at=NULL;

-- Retry only validation failures after adding model correction feedback.
UPDATE offhours_summary_tasks SET status='queued',attempts=0,scheduled_at=NOW(),lease_until=NULL
WHERE contact_id IS NOT NULL AND is_current AND status IN ('queued','failed')
AND error_message LIKE '证据校验未完成%';

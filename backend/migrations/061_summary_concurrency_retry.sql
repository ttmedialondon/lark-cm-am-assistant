-- Resume the infrastructure failure identified during release verification.
UPDATE offhours_summary_tasks SET status='queued',attempts=0,scheduled_at=NOW(),lease_until=NULL
WHERE contact_id IS NOT NULL AND is_current AND status='failed'
AND error_message='消息总结未完成，请检查服务状态。';

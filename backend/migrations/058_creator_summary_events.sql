-- Creator-scoped tasks share the existing admission, retry and revocation queue.
ALTER TABLE offhours_summary_tasks ALTER COLUMN chat_id DROP NOT NULL;
ALTER TABLE offhours_summary_tasks ADD COLUMN contact_id UUID REFERENCES contacts(id),
    ADD COLUMN checkpoint_ciphertext TEXT,
    ADD COLUMN processed_messages INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN total_messages INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX offhours_summary_creator_input_idx
    ON offhours_summary_tasks(run_id,contact_id,input_fingerprint) WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX offhours_summary_creator_current_idx
    ON offhours_summary_tasks(run_id,contact_id) WHERE is_current AND contact_id IS NOT NULL;
-- Retain the old reports for audit, rebuild the visible seven-day periods.
UPDATE offhours_summary_tasks t SET is_current=FALSE,
    status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,
    lease_until=NULL
    FROM offhours_summary_runs r WHERE r.id=t.run_id AND r.period IS NOT NULL;
UPDATE offhours_summary_runs SET next_check_at=NOW(),refresh_until=GREATEST(refresh_until,NOW()+INTERVAL '1 day'),
    coverage='{}' WHERE period IS NOT NULL
    AND report_date >= (NOW() AT TIME ZONE 'Europe/London')::date-6;

ALTER TABLE offhours_summary_settings
    ADD COLUMN recurring_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN next_period_check_at TIMESTAMPTZ;
ALTER TABLE offhours_summary_runs
    ADD COLUMN report_date DATE,
    ADD COLUMN period TEXT CHECK (period IN ('work','off')),
    ADD COLUMN next_check_at TIMESTAMPTZ,
    ADD COLUMN refresh_until TIMESTAMPTZ;
CREATE UNIQUE INDEX offhours_summary_period_idx
    ON offhours_summary_runs(responsible_authorization_id,report_date,period)
    WHERE period IS NOT NULL;

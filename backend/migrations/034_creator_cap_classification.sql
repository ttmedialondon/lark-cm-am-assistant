-- Unknown is not non-CAP. Do not relabel or repopulate historical records.
ALTER TABLE creator_workflow_candidates ADD COLUMN creator_type TEXT
    CHECK (creator_type IN ('cap', 'non_cap'));
ALTER TABLE creator_account_assignments ADD COLUMN creator_type TEXT
    CHECK (creator_type IN ('cap', 'non_cap'));
ALTER TABLE creator_account_assignments DROP CONSTRAINT creator_account_assignments_source_check;
ALTER TABLE creator_account_assignments ADD CONSTRAINT creator_account_assignments_source_check
    CHECK (source IN ('cm_confirmation', 'excel_verified', 'excel_ai_verified'));

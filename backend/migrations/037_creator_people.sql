-- A confirmed creator is a person/CM relationship; a social account is optional.
ALTER TABLE creator_account_assignments ALTER COLUMN social_account_id DROP NOT NULL;
ALTER TABLE creator_account_assignments DROP CONSTRAINT creator_account_assignments_source_check;
ALTER TABLE creator_account_assignments ADD CONSTRAINT creator_account_assignments_source_check
    CHECK (source IN ('cm_confirmation','excel_verified','excel_ai_verified','ai_business_verified'));

ALTER TABLE creator_workflow_candidates DROP CONSTRAINT creator_workflow_candidates_check;
ALTER TABLE creator_workflow_candidates ADD CONSTRAINT creator_workflow_candidates_check
    CHECK (status <> 'confirmed' OR (responsible_authorization_id IS NOT NULL
        AND contact_id IS NOT NULL AND relationship IS NOT NULL AND cardinality(uncertainties) = 0
        AND ((normalized_handle IS NULL) = (social_account_id IS NULL))));

CREATE UNIQUE INDEX creator_person_without_account_idx ON creator_account_assignments(contact_id)
    WHERE ended_at IS NULL AND social_account_id IS NULL;

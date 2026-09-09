-- A CM confirms the account/user association; this does not assert account ownership.
-- Existing owner/representative facts and all legacy assignments remain untouched.
ALTER TABLE contact_social_accounts DROP CONSTRAINT contact_social_accounts_relationship_check;
ALTER TABLE contact_social_accounts ADD CONSTRAINT contact_social_accounts_relationship_check
    CHECK (relationship IN ('owner', 'representative', 'associated'));

ALTER TABLE creator_workflow_candidates DROP CONSTRAINT creator_workflow_candidates_relationship_check;
ALTER TABLE creator_workflow_candidates ADD CONSTRAINT creator_workflow_candidates_relationship_check
    CHECK (relationship IN ('owner', 'representative', 'associated'));

ALTER TABLE creator_account_assignments DROP CONSTRAINT creator_account_assignments_relationship_check;
ALTER TABLE creator_account_assignments ADD CONSTRAINT creator_account_assignments_relationship_check
    CHECK (relationship IN ('owner', 'representative', 'associated'));

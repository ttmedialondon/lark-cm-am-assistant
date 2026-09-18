-- Identity stays global; each CM has an independent, idempotent responsibility.
DROP INDEX contact_assignments_one_active_contact_idx;
CREATE UNIQUE INDEX contact_assignments_one_active_pair_idx
  ON contact_assignments(contact_id,responsible_authorization_id) WHERE ended_at IS NULL;
DROP INDEX creator_account_one_current_cm_idx;
CREATE UNIQUE INDEX creator_account_one_current_pair_idx
  ON creator_account_assignments(social_account_id,responsible_authorization_id) WHERE ended_at IS NULL;
DROP INDEX creator_person_without_account_idx;
CREATE UNIQUE INDEX creator_person_without_account_idx
  ON creator_account_assignments(contact_id,responsible_authorization_id)
  WHERE ended_at IS NULL AND social_account_id IS NULL;

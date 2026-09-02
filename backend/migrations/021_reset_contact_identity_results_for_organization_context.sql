-- The owner requested a clean identity re-evaluation. These are derived
-- classifications only: raw Lark messages, stable identities, organization
-- mappings, authorizations, sync state and owner assignments remain intact.
-- The new worker code will not spend on AI until this person has a verified
-- organization fact, so every future evidence pack includes both organizations.

DELETE FROM contact_assessments;
-- contact_assessment_refs cascade with their parent assessments.
DELETE FROM contact_assignment_assessments;
-- Old AI owner-match suggestions must not suppress a fresh match after a
-- contact receives a newly verified creator category. Manual assignments stay.

DELETE FROM contact_roles;

-- Internal status is deterministic Lark tenant data, not an AI conclusion.
INSERT INTO contact_roles (contact_id, role, status, source, is_primary, reviewed_at)
SELECT DISTINCT identity.contact_id, 'internal_colleague', 'confirmed', 'lark_internal_tenant', TRUE, NOW()
FROM lark_identities identity
WHERE identity.identity_type = 'user'
  AND identity.tenant_classification = 'internal'
  AND identity.contact_id IS NOT NULL;

UPDATE ai_feature_configs
SET automatic_queue_checked_at = '-infinity'::timestamptz,
    updated_at = NOW()
WHERE feature IN ('contact_assessment', 'contact_assignment');

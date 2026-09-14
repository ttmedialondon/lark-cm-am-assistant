-- CAP is a person attribute, independent of account imports and CM ownership.
ALTER TABLE contacts ADD COLUMN creator_type TEXT NOT NULL DEFAULT 'pending'
    CHECK (creator_type IN ('cap','non_cap','pending'));

-- Preserve unambiguous, previously recorded classifications. Missing or
-- conflicting classifications stay pending; absence from a roster proves nothing.
UPDATE contacts c SET creator_type=prior.creator_type
FROM (
    SELECT contact_id,min(creator_type) AS creator_type
    FROM creator_account_assignments
    WHERE ended_at IS NULL AND creator_type IS NOT NULL
    GROUP BY contact_id HAVING count(DISTINCT creator_type)=1
) prior WHERE prior.contact_id=c.id;

-- The owner-requested history pass fills missing associations, not already-linked people.
-- Preserve paid results, usage and every business relationship. Fence stale worker results.
WITH completed AS (
    UPDATE creator_handle_jobs j SET status='completed',reason_code='already_confirmed',
        desired_version=desired_version+1,snapshot_version=desired_version+1,
        stats=stats||'{"history_association_reused":true}'::jsonb,
        claim_token=NULL,lease_until=NULL,finished_at=NOW(),updated_at=NOW()
    WHERE j.source='history' AND j.status NOT IN ('completed','cancelled')
      AND NOT EXISTS(SELECT 1 FROM creator_handle_jobs newer WHERE newer.contact_id=j.contact_id
        AND (newer.created_at,newer.id)>(j.created_at,j.id))
      AND EXISTS(SELECT 1 FROM contact_social_accounts l JOIN social_accounts s ON s.id=l.social_account_id
        WHERE l.contact_id=j.contact_id AND l.relationship='owner' AND l.status='confirmed'
          AND s.platform='tiktok')
    RETURNING j.id
)
INSERT INTO audit_events(id,action,metadata)
SELECT gen_random_uuid(),'creator_handle_history_associations_reused',
    jsonb_build_object('jobs_completed',count(*),'origin','migration_079') FROM completed HAVING count(*)>0;

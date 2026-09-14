-- Resume only the current manual CAP roster under the newly requested two-stage AI flow.
-- Previously reset/archived policies and confirmed/deferred rows retain their history.
UPDATE creator_import_rows r
SET status='pending',contact_id=NULL,outcome=NULL,reason_code='',attempts=0,
    lease_until=NULL,scheduled_at=NOW(),updated_at=NOW(),
    stats=stats||jsonb_build_object('analysis_stage','group',
        'analysis_generation',COALESCE((stats->>'analysis_generation')::int,0)+1)
FROM creator_import_batches b
WHERE b.id=r.batch_id AND b.policy_version='creator-import-cap-manual-v1'
    AND b.status<>'preview' AND r.review_status='pending';

UPDATE creator_import_batches b
SET policy_version='creator-import-v2',updated_at=NOW(),
    status=CASE WHEN status<>'preview' AND EXISTS (
        SELECT 1 FROM creator_import_rows r WHERE r.batch_id=b.id
            AND r.review_status='pending' AND r.status='pending') THEN 'running' ELSE status END
WHERE policy_version='creator-import-cap-manual-v1';

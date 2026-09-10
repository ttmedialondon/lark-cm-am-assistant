-- Retire only page grants, never creator records, message data or historical audits.
WITH removed AS (
    DELETE FROM admin_page_permissions WHERE permission IN ('creators','creator-review')
    RETURNING authorization_id,permission
)
INSERT INTO audit_events(id,authorization_id,action,metadata)
SELECT gen_random_uuid(),authorization_id,'admin_creator_pages_retired',
       jsonb_build_object('removed_permissions',jsonb_agg(permission ORDER BY permission))
FROM removed GROUP BY authorization_id;

ALTER TABLE admin_page_permissions DROP CONSTRAINT admin_page_permissions_permission_check;
ALTER TABLE admin_page_permissions ADD CONSTRAINT admin_page_permissions_permission_check
    CHECK (permission IN ('overview','users','chats','messages','contacts','ai','jobs'));

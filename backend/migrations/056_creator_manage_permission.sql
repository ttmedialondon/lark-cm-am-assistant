ALTER TABLE admin_page_permissions DROP CONSTRAINT admin_page_permissions_permission_check;
ALTER TABLE admin_page_permissions ADD CONSTRAINT admin_page_permissions_permission_check
    CHECK (permission IN ('overview','users','chats','messages','contacts','ai','jobs','creator-roster','creator-manage'));

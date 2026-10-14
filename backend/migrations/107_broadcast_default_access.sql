-- New accounts can open broadcast; existing disabled accounts and Lark send grants stay unchanged.
ALTER TABLE authorizations ALTER COLUMN broadcast_enabled SET DEFAULT TRUE;

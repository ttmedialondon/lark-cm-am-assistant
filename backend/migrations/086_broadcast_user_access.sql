-- Frontend-only access; no admin role or Lark scope is granted by this flag.
ALTER TABLE authorizations ADD COLUMN broadcast_enabled BOOLEAN NOT NULL DEFAULT FALSE;

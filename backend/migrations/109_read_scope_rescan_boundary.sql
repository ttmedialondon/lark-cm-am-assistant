-- Sending, media and profile grants do not change the existing message-reading boundary.
DROP TRIGGER creator_authorization_event ON authorizations;
CREATE TRIGGER creator_authorization_event AFTER UPDATE ON authorizations FOR EACH ROW
  WHEN(OLD.status IS DISTINCT FROM NEW.status OR OLD.body_consent_at IS DISTINCT FROM NEW.body_consent_at
    OR OLD.open_id IS DISTINCT FROM NEW.open_id
    OR (OLD.scopes @> ARRAY['im:chat:read','im:message:readonly']::text[])
      IS DISTINCT FROM (NEW.scopes @> ARRAY['im:chat:read','im:message:readonly']::text[]))
  EXECUTE FUNCTION creator_authorization_changed();

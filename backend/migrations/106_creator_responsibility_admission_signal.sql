-- New confirmations from any writer enter the same queue; existing people are not re-enrolled.
CREATE FUNCTION creator_responsibility_role_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.role='creator' AND NEW.status='confirmed' AND
    (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status OR OLD.role IS DISTINCT FROM NEW.role) THEN
    PERFORM enqueue_creator_responsibility(NEW.contact_id,'history');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_responsibility_role_event AFTER INSERT OR UPDATE ON contact_roles FOR EACH ROW
  EXECUTE FUNCTION creator_responsibility_role_changed();

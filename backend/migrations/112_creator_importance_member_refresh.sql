-- A refresh failure/retry is not a change to the saved membership or message evidence.
DROP TRIGGER creator_importance_access_edit ON authorization_chats;
CREATE TRIGGER creator_importance_access_edit AFTER UPDATE ON authorization_chats FOR EACH ROW
  WHEN(OLD.is_visible IS DISTINCT FROM NEW.is_visible OR OLD.sync_enabled IS DISTINCT FROM NEW.sync_enabled
    OR OLD.p2p_counterpart_open_id IS DISTINCT FROM NEW.p2p_counterpart_open_id
    OR OLD.member_count IS DISTINCT FROM NEW.member_count
    OR (OLD.member_snapshot_status='partial' AND NEW.member_snapshot_status='succeeded')
    OR OLD.authorization_id IS DISTINCT FROM NEW.authorization_id OR OLD.chat_id IS DISTINCT FROM NEW.chat_id)
  EXECUTE FUNCTION creator_importance_chat_changed();

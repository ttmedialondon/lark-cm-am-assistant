-- Authorization changes enqueue local work; scanning never runs in the login transaction.
CREATE TABLE creator_authorization_rescans (
  authorization_id UUID PRIMARY KEY REFERENCES authorizations(id) ON DELETE CASCADE,
  request_id UUID NOT NULL DEFAULT gen_random_uuid(),
  generation BIGINT NOT NULL DEFAULT 1 CHECK(generation>0),
  stage TEXT NOT NULL DEFAULT 'importance' CHECK(stage IN ('importance','chats')),
  contact_cursor UUID,
  chat_cursor TEXT,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_error_code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX creator_authorization_rescans_due ON creator_authorization_rescans(not_before,requested_at);

DROP TRIGGER creator_handle_authorization_event ON authorizations;
DROP TRIGGER creator_group_authorization_event ON authorizations;
DROP TRIGGER creator_importance_authorization_event ON authorizations;
DROP FUNCTION creator_handle_authorization_changed();
DROP FUNCTION creator_group_authorization_changed();
DROP FUNCTION creator_importance_authorization_changed();

CREATE FUNCTION creator_authorization_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO creator_authorization_rescans(authorization_id) VALUES(NEW.id)
  ON CONFLICT(authorization_id) DO UPDATE SET
    request_id=gen_random_uuid(),generation=creator_authorization_rescans.generation+1,stage='importance',
    contact_cursor=NULL,chat_cursor=NULL,requested_at=NOW(),not_before=NOW(),last_error_code='';
  RETURN NEW;
END $$;
CREATE TRIGGER creator_authorization_event AFTER UPDATE ON authorizations FOR EACH ROW
  WHEN(OLD.status IS DISTINCT FROM NEW.status OR OLD.body_consent_at IS DISTINCT FROM NEW.body_consent_at
    OR OLD.open_id IS DISTINCT FROM NEW.open_id OR OLD.scopes IS DISTINCT FROM NEW.scopes)
  EXECUTE FUNCTION creator_authorization_changed();

CREATE OR REPLACE FUNCTION creator_importance_chat_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME='authorization_chats' THEN
    IF TG_OP='UPDATE' THEN
      -- OAuth already queued a full CM rescan; its roster reset is not another source edit.
      IF NEW.sync_generation IS DISTINCT FROM OLD.sync_generation AND NEW.member_snapshot_status='pending'
        AND (OLD.is_visible,OLD.sync_enabled,OLD.p2p_counterpart_open_id,OLD.member_count,
             OLD.authorization_id,OLD.chat_id) IS NOT DISTINCT FROM
            (NEW.is_visible,NEW.sync_enabled,NEW.p2p_counterpart_open_id,NEW.member_count,
             NEW.authorization_id,NEW.chat_id)
        AND EXISTS(SELECT 1 FROM creator_authorization_rescans WHERE authorization_id=NEW.authorization_id)
        THEN RETURN NEW; END IF;
    END IF;
    IF TG_OP<>'INSERT' THEN PERFORM creator_importance_chat_signal(OLD.chat_id,OLD.authorization_id,TRUE); END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF TG_OP='INSERT' OR (OLD.authorization_id,OLD.chat_id) IS DISTINCT FROM (NEW.authorization_id,NEW.chat_id) THEN
      PERFORM creator_importance_chat_signal(NEW.chat_id,NEW.authorization_id,TRUE);
    END IF;
  ELSE
    PERFORM creator_importance_chat_signal(OLD.chat_id,NULL,TRUE);
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  END IF;
  RETURN NEW;
END $$;

-- Current responsibilities live in contact_assignments; retain the retired table as history only.
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM creator_account_assignments WHERE ended_at IS NULL) THEN
    RAISE EXCEPTION 'Migrate active account-level responsibilities before retiring their readers';
  END IF;
END $$;

DROP TRIGGER creator_importance_account_assignment_edit ON creator_account_assignments;
DROP TRIGGER creator_importance_account_assignment_insert ON creator_account_assignments;
DROP TRIGGER summary_account_responsibility ON creator_account_assignments;

CREATE OR REPLACE FUNCTION creator_importance_is_eligible(target UUID, cm UUID)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM authorizations a WHERE a.id=cm AND a.status='active'
    AND a.body_consent_at IS NOT NULL)
    AND EXISTS(SELECT 1 FROM contact_roles r WHERE r.contact_id=target
      AND r.role='creator' AND r.status='confirmed')
    AND EXISTS(SELECT 1 FROM contact_assignments x WHERE x.contact_id=target
      AND x.responsible_authorization_id=cm AND x.ended_at IS NULL)
$$;

CREATE OR REPLACE FUNCTION creator_importance_contact_signal(target UUID, cm UUID DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE pair RECORD;
BEGIN
  FOR pair IN
    SELECT responsible_authorization_id FROM creator_importance_profiles WHERE contact_id=target
    UNION SELECT responsible_authorization_id FROM creator_importance_jobs WHERE contact_id=target
    UNION SELECT responsible_authorization_id FROM contact_assignments WHERE contact_id=target AND ended_at IS NULL
    ORDER BY responsible_authorization_id
  LOOP
    IF cm IS NULL OR cm=pair.responsible_authorization_id THEN
      PERFORM enqueue_creator_importance(target,pair.responsible_authorization_id,TRUE);
    END IF;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION creator_importance_chat_signal(chat TEXT, cm UUID DEFAULT NULL,
  reset BOOLEAN DEFAULT FALSE, message_id TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE pair RECORD; source_match JSONB; configured BOOLEAN;
BEGIN
  configured := EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_importance'
    AND enabled AND api_key_ciphertext IS NOT NULL);
  IF NOT configured AND NOT reset THEN RETURN; END IF;
  source_match := jsonb_build_array(CASE WHEN message_id IS NULL THEN jsonb_build_object('chat_id',chat)
    ELSE jsonb_build_object('chat_id',chat,'message_id',message_id) END);
  FOR pair IN
    WITH related AS (
      SELECT i.contact_id FROM lark_identities i JOIN chat_members m ON m.open_id=i.open_id
        WHERE m.chat_id=chat AND i.identity_type='user' AND m.member_type='user'
      UNION SELECT i.contact_id FROM lark_identities i JOIN chat_messages m ON m.sender_open_id=i.open_id
        WHERE m.chat_id=chat AND i.identity_type='user'
      UNION SELECT i.contact_id FROM lark_identities i JOIN authorization_chats ac
        ON ac.p2p_counterpart_open_id=i.open_id WHERE ac.chat_id=chat AND i.identity_type='user'
    ), active_pairs AS (
      SELECT x.contact_id,x.responsible_authorization_id FROM contact_assignments x
        JOIN related r ON r.contact_id=x.contact_id WHERE x.ended_at IS NULL
    )
    SELECT p.contact_id,p.responsible_authorization_id FROM creator_importance_profiles p
      WHERE reset AND p.sources @> source_match AND (cm IS NULL OR p.responsible_authorization_id=cm)
    UNION SELECT j.contact_id,j.responsible_authorization_id FROM creator_importance_jobs j
      WHERE reset AND j.refs @> source_match AND (cm IS NULL OR j.responsible_authorization_id=cm)
    UNION SELECT p.contact_id,p.responsible_authorization_id FROM active_pairs p
      JOIN authorization_chats ac ON ac.authorization_id=p.responsible_authorization_id AND ac.chat_id=chat
      WHERE configured AND (cm IS NULL OR p.responsible_authorization_id=cm)
        AND (reset OR message_id IS NULL OR EXISTS(SELECT 1 FROM chat_messages m
          JOIN authorizations a ON a.id=p.responsible_authorization_id
          LEFT JOIN lark_identities sender ON sender.open_id=m.sender_open_id
          WHERE m.chat_id=chat AND m.message_id=creator_importance_chat_signal.message_id
            AND (m.sender_open_id=a.open_id OR sender.contact_id=p.contact_id)))
    ORDER BY contact_id,responsible_authorization_id
  LOOP
    PERFORM enqueue_creator_importance(pair.contact_id,pair.responsible_authorization_id,
      reset,chat,message_id);
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION enqueue_summary_backfill(target UUID, cm UUID DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM contact_roles WHERE contact_id=target
        AND role='creator' AND status='confirmed') THEN RETURN; END IF;
    INSERT INTO offhours_summary_backfills(contact_id,responsible_authorization_id)
    SELECT target,p.responsible_authorization_id FROM contact_assignments p
    WHERE p.contact_id=target AND p.ended_at IS NULL AND (cm IS NULL OR p.responsible_authorization_id=cm)
    ORDER BY p.responsible_authorization_id
    ON CONFLICT (contact_id,responsible_authorization_id) DO UPDATE SET
        version=offhours_summary_backfills.version+1,requested_at=NOW(),not_before=NOW();
END $$;

CREATE OR REPLACE FUNCTION summary_responsibility_added() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.ended_at IS NOT NULL THEN RETURN NEW; END IF;
    IF TG_OP='INSERT' OR OLD.ended_at IS DISTINCT FROM NEW.ended_at
        OR OLD.contact_id IS DISTINCT FROM NEW.contact_id
        OR OLD.responsible_authorization_id IS DISTINCT FROM NEW.responsible_authorization_id THEN
        PERFORM enqueue_summary_backfill(NEW.contact_id,NEW.responsible_authorization_id);
    END IF;
    RETURN NEW;
END $$;

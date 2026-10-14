-- Enrol only future discoveries/material changes; no historical identity sweep.
ALTER TABLE creator_handle_jobs
  ADD COLUMN purpose TEXT NOT NULL DEFAULT 'handles' CHECK (purpose IN ('handles','identity')),
  ADD COLUMN identity_signal_at TIMESTAMPTZ;
ALTER TABLE ai_feature_configs ADD COLUMN creator_recognition_started_at TIMESTAMPTZ;
ALTER TABLE creator_handle_requests ADD COLUMN config_fingerprint TEXT;
CREATE INDEX creator_handle_request_material ON creator_handle_requests(material_fingerprint,created_at DESC);
UPDATE ai_feature_configs SET creator_recognition_started_at=NOW() WHERE feature='creator_handle_matching';

CREATE FUNCTION creator_identity_eligible(target UUID) RETURNS BOOLEAN LANGUAGE SQL AS $$
  SELECT EXISTS(SELECT 1 FROM contacts c WHERE c.id=target AND c.entity_type IN ('person','unknown')
    AND EXISTS(SELECT 1 FROM lark_identities i WHERE i.contact_id=c.id AND i.identity_type='user')
    AND NOT EXISTS(SELECT 1 FROM lark_identities i WHERE i.contact_id=c.id AND i.tenant_classification='internal')
    AND NOT EXISTS(SELECT 1 FROM lark_identities i JOIN authorizations a ON a.open_id=i.open_id
      WHERE i.contact_id=c.id)
    AND NOT EXISTS(SELECT 1 FROM contact_roles r WHERE r.contact_id=c.id
      AND (r.status='confirmed' OR r.role='creator' AND r.status='rejected')))
$$;

CREATE FUNCTION enqueue_creator_identity(target UUID, signal_at TIMESTAMPTZ DEFAULT NULL,
                                         immediate BOOLEAN DEFAULT FALSE)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID; started TIMESTAMPTZ;
BEGIN
  SELECT creator_recognition_started_at INTO started FROM ai_feature_configs
    WHERE feature='creator_handle_matching' AND enabled AND provider='openai' AND api_key_ciphertext IS NOT NULL;
  IF started IS NULL OR NOT creator_identity_eligible(target) THEN RETURN NULL; END IF;
  IF NOT EXISTS(SELECT 1 FROM contacts c WHERE c.id=target AND c.created_at>=started)
    AND (signal_at IS NULL OR signal_at<started) THEN RETURN NULL; END IF;
  INSERT INTO creator_handle_jobs(contact_id,purpose,identity_signal_at,not_before)
  VALUES(target,'identity',signal_at,NOW()+CASE WHEN immediate THEN INTERVAL '0 seconds' ELSE INTERVAL '30 seconds' END)
  ON CONFLICT(contact_id) WHERE status NOT IN ('completed','failed','cancelled') DO UPDATE SET
    desired_version=creator_handle_jobs.desired_version+1,
    identity_signal_at=EXCLUDED.identity_signal_at,
    status=CASE WHEN creator_handle_jobs.status IN ('preparing','running') THEN creator_handle_jobs.status ELSE 'queued' END,
    not_before=CASE WHEN immediate THEN NOW() ELSE LEAST(NOW()+INTERVAL '30 seconds',
      creator_handle_jobs.debounce_started_at+INTERVAL '2 minutes') END,
    updated_at=NOW()
  WHERE creator_handle_jobs.purpose='identity' AND EXCLUDED.identity_signal_at IS NOT NULL
    AND (creator_handle_jobs.identity_signal_at IS NULL OR EXCLUDED.identity_signal_at>creator_handle_jobs.identity_signal_at)
  RETURNING id INTO job;
  RETURN job;
END $$;

CREATE FUNCTION creator_identity_person_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.contact_id IS NOT NULL AND (TG_OP='INSERT' OR OLD.contact_id IS DISTINCT FROM NEW.contact_id)
    THEN PERFORM enqueue_creator_identity(NEW.contact_id); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_identity_person_event AFTER INSERT OR UPDATE OF contact_id ON lark_identities
  FOR EACH ROW EXECUTE FUNCTION creator_identity_person_changed();

CREATE FUNCTION creator_identity_material_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target UUID; changed TIMESTAMPTZ;
BEGIN
  SELECT contact_id INTO target FROM lark_identities WHERE open_id=NEW.sender_open_id AND identity_type='user';
  IF target IS NULL THEN RETURN NEW; END IF;
  -- Historic first-body hydration is not a new-message event.
  changed := CASE WHEN OLD.body_fingerprint IS NOT NULL AND OLD.body_fingerprint IS DISTINCT FROM NEW.body_fingerprint
    OR OLD.body_fingerprint IS NULL AND NEW.body_fingerprint IS NOT NULL AND EXISTS(
      SELECT 1 FROM contacts c JOIN ai_feature_configs f ON f.feature='creator_handle_matching'
      JOIN creator_handle_jobs j ON j.contact_id=c.id AND j.purpose='identity'
      WHERE c.id=target AND c.created_at>=f.creator_recognition_started_at
        AND j.status='waiting_evidence' AND j.reason_code='no_text')
    THEN NOW()
    ELSE NEW.message_update_time END;
  PERFORM enqueue_creator_identity(target,changed);
  RETURN NEW;
END $$;
CREATE TRIGGER creator_identity_message_edit AFTER UPDATE ON chat_messages FOR EACH ROW
  WHEN (OLD.body_fingerprint IS DISTINCT FROM NEW.body_fingerprint
    OR OLD.message_update_time IS DISTINCT FROM NEW.message_update_time
    OR OLD.parent_id IS DISTINCT FROM NEW.parent_id OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
  EXECUTE FUNCTION creator_identity_material_changed();

CREATE FUNCTION creator_identity_group_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  PERFORM enqueue_creator_identity(i.contact_id,NOW()) FROM lark_identities i
    WHERE i.contact_id IS NOT NULL AND i.identity_type='user' AND (
      EXISTS(SELECT 1 FROM chat_members m WHERE m.chat_id=NEW.chat_id AND m.open_id=i.open_id)
      OR EXISTS(SELECT 1 FROM chat_messages m WHERE m.chat_id=NEW.chat_id AND m.sender_open_id=i.open_id));
  RETURN NEW;
END $$;
CREATE TRIGGER creator_identity_group_edit AFTER UPDATE ON chats FOR EACH ROW
  WHEN (OLD.name IS NOT NULL AND btrim(OLD.name)<>'' AND OLD.name IS DISTINCT FROM NEW.name)
  EXECUTE FUNCTION creator_identity_group_changed();

CREATE OR REPLACE FUNCTION enqueue_creator_handle(target UUID, origin TEXT DEFAULT 'incremental', immediate BOOLEAN DEFAULT FALSE)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_handle_matching'
    AND enabled AND api_key_ciphertext IS NOT NULL) THEN RETURN NULL; END IF;
  IF NOT EXISTS(SELECT 1 FROM contact_roles WHERE contact_id=target AND role='creator'
    AND status='confirmed') THEN RETURN NULL; END IF;
  -- The identity writer can submit its same-call account results without invalidating itself.
  SELECT id INTO job FROM creator_handle_jobs WHERE contact_id=target AND purpose='identity'
    AND status IN ('preparing','running');
  IF job IS NOT NULL THEN RETURN job; END IF;
  INSERT INTO creator_handle_jobs(contact_id,source,not_before)
  VALUES(target,origin,NOW()+CASE WHEN immediate THEN INTERVAL '0 seconds' ELSE INTERVAL '30 seconds' END)
  ON CONFLICT(contact_id) WHERE status NOT IN ('completed','failed','cancelled') DO UPDATE SET
    desired_version=creator_handle_jobs.desired_version+1,purpose='handles',
    source=CASE WHEN EXCLUDED.source='history' THEN creator_handle_jobs.source ELSE EXCLUDED.source END,
    status=CASE WHEN creator_handle_jobs.status IN ('preparing','running') THEN creator_handle_jobs.status ELSE 'queued' END,
    not_before=CASE WHEN immediate THEN NOW() ELSE LEAST(NOW()+INTERVAL '30 seconds',
      creator_handle_jobs.debounce_started_at+INTERVAL '2 minutes') END,
    updated_at=NOW()
  RETURNING id INTO job;
  RETURN job;
END $$;

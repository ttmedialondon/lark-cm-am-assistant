-- Chat-only cooperation facts; no identity, handle or responsibility mutation.
ALTER TABLE chat_messages ADD COLUMN body_fingerprint TEXT
  CHECK(body_fingerprint IS NULL OR body_fingerprint ~ '^[0-9a-f]{64}$');
INSERT INTO ai_feature_configs(feature,provider,project_id,model)
VALUES('creator_importance','openai','unconfigured','gpt-5.6-luna');

CREATE TABLE creator_importance_profiles (
  contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
  auto_tier TEXT NOT NULL DEFAULT 'pending' CHECK(auto_tier IN ('focus','normal','pending')),
  phase TEXT NOT NULL DEFAULT 'unknown' CHECK(phase IN ('ongoing','single','history','intent','unknown')),
  last_substantive_progress_at TIMESTAMPTZ,
  facts_ciphertext TEXT,
  sources JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(sources)='array'),
  auto_valid BOOLEAN NOT NULL DEFAULT FALSE,
  manual_tier TEXT CHECK(manual_tier IN ('focus','normal','pending')),
  manual_reason_ciphertext TEXT,
  coverage JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(coverage)='object'),
  window_start TIMESTAMPTZ,
  cutoff TIMESTAMPTZ,
  evaluated_at TIMESTAMPTZ,
  prompt_version TEXT,
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
  PRIMARY KEY(contact_id,responsible_authorization_id)
);
CREATE INDEX creator_importance_profiles_cm ON creator_importance_profiles(responsible_authorization_id,contact_id);
CREATE INDEX creator_importance_profiles_sources ON creator_importance_profiles USING GIN(sources jsonb_path_ops);

CREATE TABLE creator_importance_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
    ('queued','preparing','ready','running','retry_wait','completed','failed','cancelled')),
  desired_version INTEGER NOT NULL DEFAULT 1 CHECK(desired_version>0),
  snapshot_version INTEGER,
  pending_messages JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(pending_messages)='object'),
  needs_history BOOLEAN NOT NULL DEFAULT TRUE,
  history_start TIMESTAMPTZ,
  history_end TIMESTAMPTZ,
  cursor_time TIMESTAMPTZ,
  cursor_chat_id TEXT,
  cursor_message_id TEXT,
  checkpoint_ciphertext TEXT,
  pack_ciphertext TEXT,
  refs JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(refs)='array'),
  fingerprint TEXT,
  config_fingerprint TEXT,
  stats JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(stats)='object'),
  reason_code TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
  claim_token UUID,
  lease_until TIMESTAMPTZ,
  not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  debounce_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  prepared_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(contact_id,responsible_authorization_id)
);
CREATE INDEX creator_importance_jobs_due ON creator_importance_jobs(status,not_before,created_at);
CREATE INDEX creator_importance_jobs_cm ON creator_importance_jobs(responsible_authorization_id,contact_id);
CREATE INDEX creator_importance_jobs_refs ON creator_importance_jobs USING GIN(refs jsonb_path_ops);

CREATE TABLE creator_importance_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID REFERENCES creator_importance_jobs(id) ON DELETE SET NULL,
  key_feature TEXT NOT NULL DEFAULT 'creator_importance',
  fingerprint TEXT NOT NULL,
  request_id TEXT,
  response_id TEXT,
  error_code TEXT NOT NULL DEFAULT '',
  input_tokens BIGINT CHECK(input_tokens>=0),
  output_tokens BIGINT CHECK(output_tokens>=0),
  response_ciphertext TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX creator_importance_request_job ON creator_importance_requests(job_id,created_at DESC);
CREATE INDEX creator_importance_request_fingerprint ON creator_importance_requests(job_id,fingerprint)
  WHERE response_ciphertext IS NOT NULL;

CREATE FUNCTION creator_importance_is_eligible(target UUID, cm UUID)
RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM authorizations a WHERE a.id=cm AND a.status='active'
    AND a.body_consent_at IS NOT NULL)
    AND EXISTS(SELECT 1 FROM contact_roles r WHERE r.contact_id=target
      AND r.role='creator' AND r.status='confirmed')
    AND (EXISTS(SELECT 1 FROM contact_assignments x WHERE x.contact_id=target
      AND x.responsible_authorization_id=cm AND x.ended_at IS NULL)
      OR EXISTS(SELECT 1 FROM creator_account_assignments x WHERE x.contact_id=target
        AND x.responsible_authorization_id=cm AND x.ended_at IS NULL AND x.relationship='owner'))
$$;

CREATE FUNCTION enqueue_creator_importance(target UUID, cm UUID, reset BOOLEAN DEFAULT FALSE,
  message_chat TEXT DEFAULT NULL, message_id TEXT DEFAULT NULL)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID; configured BOOLEAN; pending JSONB;
BEGIN
  IF target IS NULL OR cm IS NULL THEN RETURN NULL; END IF;
  configured := EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_importance'
    AND enabled AND api_key_ciphertext IS NOT NULL);
  IF reset THEN
    -- Source writers acquire job before profile; consumers must validate source rows first.
    UPDATE creator_importance_jobs SET desired_version=desired_version+1,status='cancelled',
      needs_history=TRUE,history_start=NULL,history_end=NULL,cursor_time=NULL,cursor_chat_id=NULL,
      cursor_message_id=NULL,pack_ciphertext=NULL,refs='[]',fingerprint=NULL,
      snapshot_version=NULL,claim_token=NULL,lease_until=NULL,attempts=0,reason_code='source_changed',
      not_before=NOW(),updated_at=NOW()
    WHERE contact_id=target AND responsible_authorization_id=cm RETURNING id INTO job;
    UPDATE creator_importance_profiles SET auto_valid=FALSE,revision=revision+1
    WHERE contact_id=target AND responsible_authorization_id=cm;
  END IF;
  IF NOT configured OR NOT creator_importance_is_eligible(target,cm) THEN RETURN job; END IF;
  pending := CASE WHEN message_chat IS NOT NULL AND message_id IS NOT NULL
    THEN jsonb_build_object(message_chat||'/'||message_id,
      jsonb_build_object('chat_id',message_chat,'message_id',message_id)) ELSE '{}'::jsonb END;
  INSERT INTO creator_importance_jobs(contact_id,responsible_authorization_id,pending_messages,not_before)
  VALUES(target,cm,pending,NOW()+CASE WHEN reset THEN INTERVAL '0 seconds' ELSE INTERVAL '30 seconds' END)
  ON CONFLICT(contact_id,responsible_authorization_id) DO UPDATE SET
    pending_messages=creator_importance_jobs.pending_messages||EXCLUDED.pending_messages,
    -- New messages must not reopen a failed request or bypass a frozen batch's retry limit.
    status=CASE WHEN creator_importance_jobs.status IN ('preparing','running','ready','retry_wait','failed')
      THEN creator_importance_jobs.status ELSE 'queued' END,
    attempts=CASE WHEN creator_importance_jobs.status IN ('completed','cancelled')
      THEN 0 ELSE creator_importance_jobs.attempts END,
    not_before=CASE WHEN reset THEN NOW()
      WHEN creator_importance_jobs.status IN ('preparing','running','ready','retry_wait','failed')
        THEN creator_importance_jobs.not_before
      WHEN creator_importance_jobs.status IN ('completed','cancelled') THEN NOW()+INTERVAL '30 seconds'
      ELSE LEAST(NOW()+INTERVAL '30 seconds',creator_importance_jobs.debounce_started_at+INTERVAL '2 minutes') END,
    debounce_started_at=CASE WHEN creator_importance_jobs.status IN ('completed','cancelled')
      THEN NOW() ELSE creator_importance_jobs.debounce_started_at END,
    updated_at=NOW()
  RETURNING id INTO job;
  RETURN job;
END $$;

CREATE FUNCTION creator_importance_contact_signal(target UUID, cm UUID DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE pair RECORD;
BEGIN
  FOR pair IN
    SELECT responsible_authorization_id FROM creator_importance_profiles WHERE contact_id=target
    UNION SELECT responsible_authorization_id FROM creator_importance_jobs WHERE contact_id=target
    UNION SELECT responsible_authorization_id FROM contact_assignments WHERE contact_id=target AND ended_at IS NULL
    UNION SELECT responsible_authorization_id FROM creator_account_assignments
      WHERE contact_id=target AND ended_at IS NULL AND relationship='owner'
    ORDER BY responsible_authorization_id
  LOOP
    IF cm IS NULL OR cm=pair.responsible_authorization_id THEN
      PERFORM enqueue_creator_importance(target,pair.responsible_authorization_id,TRUE);
    END IF;
  END LOOP;
END $$;

CREATE FUNCTION creator_importance_chat_signal(chat TEXT, cm UUID DEFAULT NULL,
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
      UNION SELECT x.contact_id,x.responsible_authorization_id FROM creator_account_assignments x
        JOIN related r ON r.contact_id=x.contact_id WHERE x.ended_at IS NULL AND x.relationship='owner'
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

CREATE FUNCTION creator_importance_observation_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE consent TIMESTAMPTZ;
BEGIN
  IF TG_OP='DELETE' THEN
    PERFORM creator_importance_chat_signal(OLD.chat_id,OLD.authorization_id,TRUE,OLD.message_id);
    RETURN OLD;
  END IF;
  IF TG_OP='UPDATE' THEN
    IF (OLD.authorization_id,OLD.chat_id,OLD.message_id) IS DISTINCT FROM
      (NEW.authorization_id,NEW.chat_id,NEW.message_id) THEN
      PERFORM creator_importance_chat_signal(OLD.chat_id,OLD.authorization_id,TRUE,OLD.message_id);
    ELSE
      SELECT body_consent_at INTO consent FROM authorizations WHERE id=NEW.authorization_id;
      IF OLD.sent_by_author IS NOT DISTINCT FROM NEW.sent_by_author
        AND NOT (consent IS NOT NULL AND OLD.observed_at<consent AND NEW.observed_at>=consent) THEN RETURN NEW; END IF;
    END IF;
  END IF;
  PERFORM creator_importance_chat_signal(NEW.chat_id,NEW.authorization_id,FALSE,NEW.message_id);
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_observation_event AFTER INSERT OR UPDATE OR DELETE
  ON authorization_message_observations FOR EACH ROW EXECUTE FUNCTION creator_importance_observation_changed();

CREATE FUNCTION creator_importance_message_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  PERFORM creator_importance_chat_signal(OLD.chat_id,NULL,TRUE,OLD.message_id);
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  IF OLD.chat_id IS DISTINCT FROM NEW.chat_id OR OLD.message_id IS DISTINCT FROM NEW.message_id THEN
    PERFORM creator_importance_chat_signal(NEW.chat_id,NULL,FALSE,NEW.message_id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_message_edit AFTER UPDATE ON chat_messages FOR EACH ROW
  WHEN(OLD.message_update_time IS DISTINCT FROM NEW.message_update_time OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
    OR (OLD.body_fingerprint IS NOT NULL AND OLD.body_fingerprint IS DISTINCT FROM NEW.body_fingerprint)
    OR (OLD.body_ciphertext IS NULL) IS DISTINCT FROM (NEW.body_ciphertext IS NULL)
    OR OLD.sender_open_id IS DISTINCT FROM NEW.sender_open_id OR OLD.sender_type IS DISTINCT FROM NEW.sender_type
    OR OLD.message_type IS DISTINCT FROM NEW.message_type OR OLD.message_create_time IS DISTINCT FROM NEW.message_create_time
    OR OLD.parent_id IS DISTINCT FROM NEW.parent_id OR OLD.root_id IS DISTINCT FROM NEW.root_id
    OR OLD.thread_id IS DISTINCT FROM NEW.thread_id OR OLD.mentions_json IS DISTINCT FROM NEW.mentions_json
    OR OLD.chat_id IS DISTINCT FROM NEW.chat_id OR OLD.message_id IS DISTINCT FROM NEW.message_id)
  EXECUTE FUNCTION creator_importance_message_changed();
CREATE TRIGGER creator_importance_message_delete AFTER DELETE ON chat_messages FOR EACH ROW
  EXECUTE FUNCTION creator_importance_message_changed();

CREATE FUNCTION creator_importance_chat_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME='authorization_chats' THEN
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
CREATE TRIGGER creator_importance_access_insert AFTER INSERT ON authorization_chats FOR EACH ROW
  EXECUTE FUNCTION creator_importance_chat_changed();
CREATE TRIGGER creator_importance_access_edit AFTER UPDATE ON authorization_chats FOR EACH ROW
  WHEN(OLD.is_visible IS DISTINCT FROM NEW.is_visible OR OLD.sync_enabled IS DISTINCT FROM NEW.sync_enabled
    OR OLD.p2p_counterpart_open_id IS DISTINCT FROM NEW.p2p_counterpart_open_id
    OR OLD.member_snapshot_status IS DISTINCT FROM NEW.member_snapshot_status
    OR OLD.member_count IS DISTINCT FROM NEW.member_count
    OR OLD.authorization_id IS DISTINCT FROM NEW.authorization_id OR OLD.chat_id IS DISTINCT FROM NEW.chat_id)
  EXECUTE FUNCTION creator_importance_chat_changed();
CREATE TRIGGER creator_importance_access_delete AFTER DELETE ON authorization_chats FOR EACH ROW
  EXECUTE FUNCTION creator_importance_chat_changed();
CREATE TRIGGER creator_importance_chat_edit AFTER UPDATE ON chats FOR EACH ROW
  WHEN(OLD.name IS DISTINCT FROM NEW.name OR OLD.chat_mode IS DISTINCT FROM NEW.chat_mode
    OR OLD.chat_status IS DISTINCT FROM NEW.chat_status)
  EXECUTE FUNCTION creator_importance_chat_changed();

CREATE FUNCTION creator_importance_membership_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN PERFORM creator_importance_chat_signal(OLD.chat_id,NULL,TRUE); END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  IF TG_OP='INSERT' OR OLD.chat_id IS DISTINCT FROM NEW.chat_id THEN
    PERFORM creator_importance_chat_signal(NEW.chat_id,NULL,TRUE);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_membership_insert AFTER INSERT OR DELETE ON chat_members FOR EACH ROW
  EXECUTE FUNCTION creator_importance_membership_changed();
CREATE TRIGGER creator_importance_membership_edit AFTER UPDATE ON chat_members FOR EACH ROW
  WHEN(OLD.chat_id IS DISTINCT FROM NEW.chat_id OR OLD.open_id IS DISTINCT FROM NEW.open_id
    OR OLD.member_type IS DISTINCT FROM NEW.member_type)
  EXECUTE FUNCTION creator_importance_membership_changed();

CREATE FUNCTION creator_importance_authorization_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE pair RECORD;
BEGIN
  FOR pair IN
    SELECT contact_id FROM creator_importance_profiles WHERE responsible_authorization_id=NEW.id
    UNION SELECT contact_id FROM creator_importance_jobs WHERE responsible_authorization_id=NEW.id
    UNION SELECT contact_id FROM contact_assignments WHERE responsible_authorization_id=NEW.id AND ended_at IS NULL
    UNION SELECT contact_id FROM creator_account_assignments WHERE responsible_authorization_id=NEW.id
      AND ended_at IS NULL AND relationship='owner'
    ORDER BY contact_id
  LOOP PERFORM enqueue_creator_importance(pair.contact_id,NEW.id,TRUE); END LOOP;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_authorization_event AFTER UPDATE ON authorizations FOR EACH ROW
  WHEN(OLD.status IS DISTINCT FROM NEW.status OR OLD.body_consent_at IS DISTINCT FROM NEW.body_consent_at
    OR OLD.open_id IS DISTINCT FROM NEW.open_id)
  EXECUTE FUNCTION creator_importance_authorization_changed();

CREATE FUNCTION creator_importance_assignment_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN
    PERFORM enqueue_creator_importance(OLD.contact_id,OLD.responsible_authorization_id,TRUE);
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  IF TG_OP='INSERT' OR (OLD.contact_id,OLD.responsible_authorization_id) IS DISTINCT FROM
    (NEW.contact_id,NEW.responsible_authorization_id) THEN
    PERFORM enqueue_creator_importance(NEW.contact_id,NEW.responsible_authorization_id,TRUE);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_person_assignment_insert AFTER INSERT OR DELETE ON contact_assignments FOR EACH ROW
  EXECUTE FUNCTION creator_importance_assignment_changed();
CREATE TRIGGER creator_importance_person_assignment_edit AFTER UPDATE ON contact_assignments FOR EACH ROW
  WHEN(OLD.ended_at IS DISTINCT FROM NEW.ended_at OR OLD.contact_id IS DISTINCT FROM NEW.contact_id
    OR OLD.responsible_authorization_id IS DISTINCT FROM NEW.responsible_authorization_id)
  EXECUTE FUNCTION creator_importance_assignment_changed();
CREATE TRIGGER creator_importance_account_assignment_insert AFTER INSERT OR DELETE ON creator_account_assignments FOR EACH ROW
  EXECUTE FUNCTION creator_importance_assignment_changed();
CREATE TRIGGER creator_importance_account_assignment_edit AFTER UPDATE ON creator_account_assignments FOR EACH ROW
  WHEN(OLD.ended_at IS DISTINCT FROM NEW.ended_at OR OLD.relationship IS DISTINCT FROM NEW.relationship
    OR OLD.contact_id IS DISTINCT FROM NEW.contact_id OR OLD.responsible_authorization_id IS DISTINCT FROM NEW.responsible_authorization_id)
  EXECUTE FUNCTION creator_importance_assignment_changed();

CREATE FUNCTION creator_importance_identity_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE material RECORD;
BEGIN
  IF TG_OP<>'INSERT' THEN PERFORM creator_importance_contact_signal(OLD.contact_id); END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  IF TG_OP='INSERT' OR OLD.contact_id IS DISTINCT FROM NEW.contact_id THEN
    PERFORM creator_importance_contact_signal(NEW.contact_id);
  END IF;
  IF TG_OP='UPDATE' THEN
    -- Mention target mapping can change without changing the raw message or its HMAC.
    FOR material IN
      SELECT DISTINCT m.chat_id,m.message_id FROM creator_importance_profiles p
      CROSS JOIN LATERAL jsonb_array_elements(p.sources) source
      JOIN chat_messages m ON m.chat_id=source->>'chat_id' AND m.message_id=source->>'message_id'
      WHERE m.mentions_json @> jsonb_build_array(jsonb_build_object('id',NEW.open_id))
      UNION SELECT DISTINCT m.chat_id,m.message_id FROM creator_importance_jobs j
      CROSS JOIN LATERAL jsonb_array_elements(j.refs) source
      JOIN chat_messages m ON m.chat_id=source->>'chat_id' AND m.message_id=source->>'message_id'
      WHERE m.mentions_json @> jsonb_build_array(jsonb_build_object('id',NEW.open_id))
      ORDER BY chat_id,message_id
    LOOP PERFORM creator_importance_chat_signal(material.chat_id,NULL,TRUE,material.message_id); END LOOP;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_identity_insert AFTER INSERT OR DELETE ON lark_identities FOR EACH ROW
  EXECUTE FUNCTION creator_importance_identity_changed();
CREATE TRIGGER creator_importance_identity_edit AFTER UPDATE ON lark_identities FOR EACH ROW
  WHEN(OLD.contact_id IS DISTINCT FROM NEW.contact_id OR OLD.identity_type IS DISTINCT FROM NEW.identity_type)
  EXECUTE FUNCTION creator_importance_identity_changed();

CREATE FUNCTION creator_importance_role_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP<>'INSERT' AND OLD.role='creator' THEN PERFORM creator_importance_contact_signal(OLD.contact_id); END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  IF NEW.role='creator' AND (TG_OP='INSERT' OR OLD.role<>'creator' OR OLD.contact_id IS DISTINCT FROM NEW.contact_id) THEN
    PERFORM creator_importance_contact_signal(NEW.contact_id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_role_insert AFTER INSERT OR DELETE ON contact_roles FOR EACH ROW
  EXECUTE FUNCTION creator_importance_role_changed();
CREATE TRIGGER creator_importance_role_edit AFTER UPDATE ON contact_roles FOR EACH ROW
  WHEN(OLD.role IS DISTINCT FROM NEW.role OR OLD.status IS DISTINCT FROM NEW.status OR OLD.contact_id IS DISTINCT FROM NEW.contact_id)
  EXECUTE FUNCTION creator_importance_role_changed();

CREATE FUNCTION creator_importance_contact_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  PERFORM creator_importance_contact_signal(NEW.id);
  RETURN NEW;
END $$;
CREATE TRIGGER creator_importance_contact_edit AFTER UPDATE ON contacts FOR EACH ROW
  WHEN(OLD.entity_type IS DISTINCT FROM NEW.entity_type)
  EXECUTE FUNCTION creator_importance_contact_changed();

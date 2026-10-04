-- Independent, opt-in group-purpose suggestions. Existing roles and assignments are unchanged.
INSERT INTO ai_feature_configs(feature,provider,project_id,model)
VALUES ('creator_group_classification','openai','unconfigured','gpt-5.6-luna');

CREATE TABLE creator_group_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE RESTRICT,
  source TEXT NOT NULL DEFAULT 'incremental' CHECK(source IN ('incremental','history','manual')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
    ('queued','preparing','ready','running','retry_wait','waiting_evidence','completed','failed','cancelled')),
  desired_version INTEGER NOT NULL DEFAULT 1,
  snapshot_version INTEGER,
  fingerprint TEXT,
  config_fingerprint TEXT,
  pack_ciphertext TEXT,
  result_ciphertext TEXT,
  refs JSONB NOT NULL DEFAULT '[]',
  stats JSONB NOT NULL DEFAULT '{}',
  reason_code TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
  claim_token UUID,
  lease_until TIMESTAMPTZ,
  not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  debounce_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  prepared_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX creator_group_one_open_job ON creator_group_jobs(chat_id)
 WHERE status NOT IN ('completed','failed','cancelled');
CREATE INDEX creator_group_claim ON creator_group_jobs(status,not_before,created_at);
CREATE INDEX creator_group_latest ON creator_group_jobs(chat_id,created_at DESC,id DESC);
CREATE INDEX creator_group_cache ON creator_group_jobs(chat_id,fingerprint)
 WHERE result_ciphertext IS NOT NULL AND status='completed';

CREATE TABLE creator_group_requests (
  id UUID PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES creator_group_jobs(id) ON DELETE RESTRICT,
  key_feature TEXT NOT NULL DEFAULT 'creator_group_classification',
  request_id TEXT,
  response_id TEXT,
  error_code TEXT NOT NULL DEFAULT '',
  input_tokens BIGINT,
  output_tokens BIGINT,
  response_ciphertext TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX creator_group_request_job ON creator_group_requests(job_id,created_at DESC);

CREATE TABLE chat_group_classifications (
  chat_id TEXT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE RESTRICT,
  purpose TEXT NOT NULL CHECK(purpose IN ('support','community','training','category','campaign','unknown')),
  tags JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(tags)='array'),
  locked BOOLEAN NOT NULL DEFAULT TRUE,
  source_job_id UUID REFERENCES creator_group_jobs(id) ON DELETE RESTRICT,
  confirmed_by_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE RESTRICT,
  confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE FUNCTION creator_group_is_eligible(target TEXT) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM chats c WHERE c.chat_id=target
    AND COALESCE(c.chat_status,'normal')='normal' AND COALESCE(c.chat_mode,'group')<>'p2p'
    AND EXISTS(SELECT 1 FROM authorization_chats ac JOIN authorizations a ON a.id=ac.authorization_id
      WHERE ac.chat_id=c.chat_id AND ac.is_visible AND ac.sync_enabled
        AND a.status='active' AND a.body_consent_at IS NOT NULL)
    AND EXISTS(SELECT 1 FROM lark_identities i JOIN contact_roles r ON r.contact_id=i.contact_id
      WHERE r.role='creator' AND r.status='confirmed' AND i.identity_type='user'
        AND i.contact_id<>'6abf6bf9-12dc-4a22-9654-2010aa4c7785'::uuid
        AND (EXISTS(SELECT 1 FROM chat_members m WHERE m.chat_id=c.chat_id AND m.open_id=i.open_id)
          OR EXISTS(SELECT 1 FROM chat_messages m WHERE m.chat_id=c.chat_id
            AND m.sender_open_id=i.open_id AND NOT m.is_deleted))))
$$;

CREATE FUNCTION enqueue_creator_group(target TEXT, origin TEXT DEFAULT 'incremental', force BOOLEAN DEFAULT FALSE)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_group_classification'
    AND enabled AND api_key_ciphertext IS NOT NULL) OR NOT creator_group_is_eligible(target)
    OR (NOT force AND EXISTS(SELECT 1 FROM chat_group_classifications WHERE chat_id=target AND locked))
    THEN RETURN NULL; END IF;
  INSERT INTO creator_group_jobs(chat_id,source,not_before)
  VALUES(target,origin,NOW()+CASE WHEN origin='incremental' THEN INTERVAL '30 seconds' ELSE INTERVAL '0 seconds' END)
  ON CONFLICT(chat_id) WHERE status NOT IN ('completed','failed','cancelled') DO UPDATE SET
    desired_version=creator_group_jobs.desired_version+1,
    source=CASE WHEN origin='manual' THEN origin ELSE creator_group_jobs.source END,
    status=CASE WHEN creator_group_jobs.status IN ('preparing','running') THEN creator_group_jobs.status ELSE 'queued' END,
    not_before=CASE WHEN origin='incremental' THEN LEAST(NOW()+INTERVAL '30 seconds',
      creator_group_jobs.debounce_started_at+INTERVAL '2 minutes') ELSE NOW() END,
    updated_at=NOW()
  RETURNING id INTO job;
  RETURN job;
END $$;

CREATE FUNCTION creator_group_material_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  PERFORM enqueue_creator_group(NEW.chat_id);
  RETURN NEW;
END $$;
CREATE TRIGGER creator_group_chat_change AFTER UPDATE ON chats FOR EACH ROW
 WHEN(OLD.name IS DISTINCT FROM NEW.name OR OLD.chat_mode IS DISTINCT FROM NEW.chat_mode
   OR OLD.chat_status IS DISTINCT FROM NEW.chat_status)
 EXECUTE FUNCTION creator_group_material_changed();
CREATE TRIGGER creator_group_access_new AFTER INSERT ON authorization_chats FOR EACH ROW
 EXECUTE FUNCTION creator_group_material_changed();
CREATE TRIGGER creator_group_access_change AFTER UPDATE ON authorization_chats FOR EACH ROW
 WHEN(OLD.is_visible IS DISTINCT FROM NEW.is_visible OR OLD.sync_enabled IS DISTINCT FROM NEW.sync_enabled)
 EXECUTE FUNCTION creator_group_material_changed();
CREATE TRIGGER creator_group_message_edit AFTER UPDATE ON chat_messages FOR EACH ROW
 WHEN(OLD.message_update_time IS DISTINCT FROM NEW.message_update_time OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
   OR OLD.parent_id IS DISTINCT FROM NEW.parent_id OR OLD.sender_open_id IS DISTINCT FROM NEW.sender_open_id)
 EXECUTE FUNCTION creator_group_material_changed();

CREATE FUNCTION creator_group_role_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.role='creator' AND NEW.status='confirmed' AND
    (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status OR OLD.role IS DISTINCT FROM NEW.role)
    AND EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_group_classification' AND enabled) THEN
    PERFORM enqueue_creator_group(related.chat_id) FROM (
      SELECT m.chat_id FROM lark_identities i JOIN chat_members m ON m.open_id=i.open_id WHERE i.contact_id=NEW.contact_id
      UNION SELECT m.chat_id FROM lark_identities i JOIN chat_messages m ON m.sender_open_id=i.open_id
        WHERE i.contact_id=NEW.contact_id AND NOT m.is_deleted
    ) related;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_group_role_event AFTER INSERT OR UPDATE ON contact_roles FOR EACH ROW
 EXECUTE FUNCTION creator_group_role_changed();

CREATE FUNCTION creator_group_authorization_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_group_classification' AND enabled) THEN
    PERFORM enqueue_creator_group(ac.chat_id) FROM authorization_chats ac WHERE ac.authorization_id=NEW.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_group_authorization_event AFTER UPDATE ON authorizations FOR EACH ROW
 WHEN(OLD.status IS DISTINCT FROM NEW.status OR OLD.body_consent_at IS DISTINCT FROM NEW.body_consent_at)
 EXECUTE FUNCTION creator_group_authorization_changed();

-- Additive, opt-in account suggestions. Existing identities/owners/CMs are untouched.
ALTER TABLE ai_feature_configs
  ADD COLUMN rpm_limit INTEGER NOT NULL DEFAULT 30 CHECK (rpm_limit BETWEEN 1 AND 10000),
  ADD COLUMN tpm_limit INTEGER NOT NULL DEFAULT 60000 CHECK (tpm_limit BETWEEN 1000 AND 10000000),
  ADD COLUMN daily_token_budget INTEGER NOT NULL DEFAULT 1000000 CHECK (daily_token_budget BETWEEN 1000 AND 100000000);
ALTER TABLE ai_feature_configs ADD COLUMN matching_catalog_version TEXT;
INSERT INTO ai_feature_configs(feature,provider,project_id,model)
VALUES ('creator_handle_matching','openai','unconfigured','gpt-5.6-luna');

CREATE TABLE creator_handle_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
  source TEXT NOT NULL DEFAULT 'incremental' CHECK (source IN ('incremental','manual','history')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
    ('queued','preparing','ready','running','retry_wait','waiting_evidence','review_required','completed','failed','cancelled')),
  desired_version INTEGER NOT NULL DEFAULT 1,
  snapshot_version INTEGER,
  fingerprint TEXT,
  config_fingerprint TEXT,
  catalog_version TEXT,
  pack_ciphertext TEXT,
  result_ciphertext TEXT,
  refs JSONB NOT NULL DEFAULT '[]',
  stats JSONB NOT NULL DEFAULT '{}',
  reason_code TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
  claim_token UUID,
  lease_until TIMESTAMPTZ,
  not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  debounce_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  response_id TEXT,
  request_id TEXT,
  prepared_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX creator_handle_one_open_job ON creator_handle_jobs(contact_id)
 WHERE status NOT IN ('completed','failed','cancelled');
CREATE INDEX creator_handle_job_claim ON creator_handle_jobs(status,not_before,created_at);
CREATE INDEX creator_handle_cache ON creator_handle_jobs(contact_id,fingerprint)
 WHERE result_ciphertext IS NOT NULL;

-- A tiny reservation ledger survives stale results and uncertain network billing.
CREATE TABLE creator_handle_requests (
  id UUID PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES creator_handle_jobs(id) ON DELETE RESTRICT,
  quota_scope TEXT NOT NULL,
  reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens>0),
  billed_tokens INTEGER CHECK (billed_tokens>=0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX creator_handle_request_budget ON creator_handle_requests(quota_scope,created_at);

CREATE FUNCTION enqueue_creator_handle(target UUID, origin TEXT DEFAULT 'incremental', immediate BOOLEAN DEFAULT FALSE)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ai_feature_configs WHERE feature='creator_handle_matching'
    AND enabled AND api_key_ciphertext IS NOT NULL) THEN RETURN NULL; END IF;
  IF NOT EXISTS (SELECT 1 FROM contact_roles WHERE contact_id=target AND role='creator'
    AND status='confirmed') THEN RETURN NULL; END IF;
  INSERT INTO creator_handle_jobs(contact_id,source,not_before)
  VALUES(target,origin,NOW()+CASE WHEN immediate THEN INTERVAL '0 seconds' ELSE INTERVAL '30 seconds' END)
  ON CONFLICT(contact_id) WHERE status NOT IN ('completed','failed','cancelled') DO UPDATE SET
    desired_version=creator_handle_jobs.desired_version+1,
    source=CASE WHEN EXCLUDED.source='history' THEN creator_handle_jobs.source ELSE EXCLUDED.source END,
    status=CASE WHEN creator_handle_jobs.status IN ('preparing','running') THEN creator_handle_jobs.status ELSE 'queued' END,
    not_before=CASE WHEN immediate THEN NOW() ELSE LEAST(NOW()+INTERVAL '30 seconds',
      creator_handle_jobs.debounce_started_at+INTERVAL '2 minutes') END,
    updated_at=NOW()
  RETURNING id INTO job;
  RETURN job;
END $$;

CREATE FUNCTION creator_handle_role_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.role='creator' AND NEW.status='confirmed' AND
    (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status OR OLD.role IS DISTINCT FROM NEW.role) THEN
    PERFORM enqueue_creator_handle(NEW.contact_id,'incremental',TRUE);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_handle_role_event AFTER INSERT OR UPDATE ON contact_roles
 FOR EACH ROW EXECUTE FUNCTION creator_handle_role_changed();

-- Edits/renames and access changes are rare; bulk new-message signals stay page-level in Python.
CREATE FUNCTION creator_handle_chat_signal(chat TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_handle_matching' AND enabled) THEN RETURN; END IF;
  PERFORM enqueue_creator_handle(p.contact_id) FROM (
    SELECT DISTINCT i.contact_id FROM lark_identities i JOIN contact_roles r ON r.contact_id=i.contact_id
    WHERE r.role='creator' AND r.status='confirmed' AND (
      EXISTS(SELECT 1 FROM chat_members m WHERE m.chat_id=chat AND m.open_id=i.open_id) OR
      EXISTS(SELECT 1 FROM chat_messages m WHERE m.chat_id=chat AND m.sender_open_id=i.open_id) OR
      EXISTS(SELECT 1 FROM authorization_chats ac WHERE ac.chat_id=chat AND ac.p2p_counterpart_open_id=i.open_id))
  ) p;
END $$;
CREATE FUNCTION creator_handle_material_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  PERFORM creator_handle_chat_signal(NEW.chat_id);
  RETURN NEW;
END $$;
CREATE TRIGGER creator_handle_message_edit AFTER UPDATE ON chat_messages FOR EACH ROW
 WHEN (OLD.message_update_time IS DISTINCT FROM NEW.message_update_time OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
   OR OLD.parent_id IS DISTINCT FROM NEW.parent_id OR OLD.sender_open_id IS DISTINCT FROM NEW.sender_open_id)
 EXECUTE FUNCTION creator_handle_material_changed();
CREATE TRIGGER creator_handle_chat_edit AFTER UPDATE ON chats FOR EACH ROW
 WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.chat_status IS DISTINCT FROM NEW.chat_status)
 EXECUTE FUNCTION creator_handle_material_changed();
CREATE TRIGGER creator_handle_chat_access_edit AFTER UPDATE ON authorization_chats FOR EACH ROW
 WHEN (OLD.is_visible IS DISTINCT FROM NEW.is_visible OR OLD.sync_enabled IS DISTINCT FROM NEW.sync_enabled
   OR OLD.p2p_counterpart_open_id IS DISTINCT FROM NEW.p2p_counterpart_open_id)
 EXECUTE FUNCTION creator_handle_material_changed();
CREATE FUNCTION creator_handle_authorization_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_handle_matching' AND enabled) THEN
    PERFORM creator_handle_chat_signal(ac.chat_id) FROM authorization_chats ac WHERE ac.authorization_id=NEW.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER creator_handle_authorization_event AFTER UPDATE ON authorizations FOR EACH ROW
 WHEN (OLD.status IS DISTINCT FROM NEW.status OR OLD.body_consent_at IS DISTINCT FROM NEW.body_consent_at)
 EXECUTE FUNCTION creator_handle_authorization_changed();

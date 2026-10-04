-- Independent, owner-started suggestions only. No assignment writer or automatic scan.
INSERT INTO ai_feature_configs(feature,provider,project_id,model)
VALUES ('creator_responsibility_matching','openai','unconfigured','gpt-5.6-luna');

CREATE TABLE creator_responsibility_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','history','sample')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
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
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
  claim_token UUID,
  lease_until TIMESTAMPTZ,
  not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  prepared_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX creator_responsibility_one_open_job ON creator_responsibility_jobs(contact_id)
 WHERE status NOT IN ('completed','failed','cancelled');
CREATE INDEX creator_responsibility_claim ON creator_responsibility_jobs(status,not_before,created_at);
CREATE INDEX creator_responsibility_cache ON creator_responsibility_jobs(contact_id,fingerprint)
 WHERE result_ciphertext IS NOT NULL;

CREATE TABLE creator_responsibility_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES creator_responsibility_jobs(id) ON DELETE RESTRICT,
  key_feature TEXT NOT NULL,
  request_id TEXT,
  response_id TEXT,
  error_code TEXT NOT NULL DEFAULT '',
  input_tokens BIGINT,
  output_tokens BIGINT,
  response_ciphertext TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX creator_responsibility_request_job ON creator_responsibility_requests(job_id,created_at DESC);

CREATE FUNCTION enqueue_creator_responsibility(target UUID, origin TEXT DEFAULT 'manual')
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ai_feature_configs WHERE feature='creator_responsibility_matching'
    AND enabled AND api_key_ciphertext IS NOT NULL) THEN RETURN NULL; END IF;
  IF NOT EXISTS (SELECT 1 FROM contact_roles WHERE contact_id=target AND role='creator'
    AND status='confirmed') THEN RETURN NULL; END IF;
  INSERT INTO creator_responsibility_jobs(contact_id,source) VALUES(target,origin)
  ON CONFLICT(contact_id) WHERE status NOT IN ('completed','failed','cancelled') DO UPDATE SET
    source=EXCLUDED.source,
    desired_version=creator_responsibility_jobs.desired_version+1,
    status=CASE WHEN creator_responsibility_jobs.status IN ('preparing','running')
      THEN creator_responsibility_jobs.status ELSE 'queued' END,
    not_before=NOW(),updated_at=NOW()
  RETURNING id INTO job;
  RETURN job;
END $$;

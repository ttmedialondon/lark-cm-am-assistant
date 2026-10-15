-- A new material round must not inherit the expired debounce window of a saved result.
CREATE OR REPLACE FUNCTION enqueue_creator_identity(target UUID, signal_at TIMESTAMPTZ DEFAULT NULL,
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
    not_before=CASE WHEN immediate THEN NOW() ELSE GREATEST(creator_handle_jobs.not_before,
      CASE WHEN creator_handle_jobs.status IN ('queued','preparing','running')
        THEN LEAST(NOW()+INTERVAL '30 seconds',creator_handle_jobs.debounce_started_at+INTERVAL '2 minutes')
        ELSE NOW()+INTERVAL '30 seconds' END) END,
    debounce_started_at=CASE WHEN creator_handle_jobs.status IN ('queued','preparing','running')
      THEN creator_handle_jobs.debounce_started_at ELSE NOW() END,
    updated_at=NOW()
  WHERE creator_handle_jobs.purpose='identity' AND EXCLUDED.identity_signal_at IS NOT NULL
    AND (creator_handle_jobs.identity_signal_at IS NULL OR EXCLUDED.identity_signal_at>creator_handle_jobs.identity_signal_at)
  RETURNING id INTO job;
  RETURN job;
END $$;

CREATE OR REPLACE FUNCTION enqueue_creator_handle(target UUID, origin TEXT DEFAULT 'incremental', immediate BOOLEAN DEFAULT FALSE)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE job UUID;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM ai_feature_configs WHERE feature='creator_handle_matching'
    AND enabled AND api_key_ciphertext IS NOT NULL) THEN RETURN NULL; END IF;
  IF NOT EXISTS(SELECT 1 FROM contact_roles WHERE contact_id=target AND role='creator'
    AND status='confirmed') THEN RETURN NULL; END IF;
  SELECT id INTO job FROM creator_handle_jobs WHERE contact_id=target AND purpose='identity'
    AND status IN ('preparing','running');
  IF job IS NOT NULL THEN RETURN job; END IF;
  INSERT INTO creator_handle_jobs(contact_id,source,not_before)
  VALUES(target,origin,NOW()+CASE WHEN immediate THEN INTERVAL '0 seconds' ELSE INTERVAL '30 seconds' END)
  ON CONFLICT(contact_id) WHERE status NOT IN ('completed','failed','cancelled') DO UPDATE SET
    desired_version=creator_handle_jobs.desired_version+1,purpose='handles',
    source=CASE WHEN EXCLUDED.source='history' THEN creator_handle_jobs.source ELSE EXCLUDED.source END,
    status=CASE WHEN creator_handle_jobs.status IN ('preparing','running') THEN creator_handle_jobs.status ELSE 'queued' END,
    not_before=CASE WHEN immediate THEN NOW() ELSE GREATEST(creator_handle_jobs.not_before,
      CASE WHEN creator_handle_jobs.status IN ('queued','preparing','running')
        THEN LEAST(NOW()+INTERVAL '30 seconds',creator_handle_jobs.debounce_started_at+INTERVAL '2 minutes')
        ELSE NOW()+INTERVAL '30 seconds' END) END,
    debounce_started_at=CASE WHEN creator_handle_jobs.status IN ('queued','preparing','running')
      THEN creator_handle_jobs.debounce_started_at ELSE NOW() END,
    updated_at=NOW()
  RETURNING id INTO job;
  RETURN job;
END $$;

-- Correct only idle incremental jobs proven to have reused an earlier prepared round's two-minute cap.
UPDATE creator_handle_jobs SET not_before=updated_at+INTERVAL '30 seconds',debounce_started_at=updated_at
WHERE status='queued' AND source='incremental' AND claim_token IS NULL AND lease_until IS NULL
  AND prepared_at=debounce_started_at AND desired_version>snapshot_version
  AND not_before=debounce_started_at+INTERVAL '2 minutes' AND updated_at>not_before;

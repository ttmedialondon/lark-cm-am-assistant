-- Keep paid-request diagnostics even when an owner clears the matching queue.
ALTER TABLE creator_handle_requests DROP CONSTRAINT creator_handle_requests_job_id_fkey;
ALTER TABLE creator_handle_requests ALTER COLUMN job_id DROP NOT NULL;
ALTER TABLE creator_handle_requests ADD CONSTRAINT creator_handle_requests_job_id_fkey
  FOREIGN KEY(job_id) REFERENCES creator_handle_jobs(id) ON DELETE SET NULL;
ALTER TABLE creator_handle_requests
  ADD COLUMN error_code TEXT NOT NULL DEFAULT '',
  ADD COLUMN request_id TEXT,
  ADD COLUMN response_id TEXT,
  ADD COLUMN input_tokens BIGINT CHECK(input_tokens>=0),
  ADD COLUMN output_tokens BIGINT CHECK(output_tokens>=0),
  ADD COLUMN response_ciphertext TEXT;

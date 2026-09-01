-- `attempts` now means actual retry attempts.  Earlier releases also counted
-- normal pagination yields, so reset only active jobs whose saved detail proves
-- that their latest requeue was a normal continuation.
UPDATE sync_jobs
SET attempts = 0,
    last_error_code = NULL,
    last_error = NULL,
    updated_at = NOW()
WHERE status IN ('queued', 'running')
  AND last_error = 'Continuing a paginated sync safely from its saved cursor.';

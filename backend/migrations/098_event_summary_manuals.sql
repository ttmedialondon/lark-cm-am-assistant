-- New reports pin their manuals; existing reports and paid tasks remain unchanged.
ALTER TABLE offhours_summary_runs
    ADD COLUMN event_rules_ciphertext TEXT,
    ADD COLUMN event_rules_digest TEXT,
    ADD CONSTRAINT offhours_summary_event_rules_check CHECK (
        (event_rules_ciphertext IS NULL AND event_rules_digest IS NULL) OR
        (event_rules_ciphertext IS NOT NULL AND event_rules_digest IS NOT NULL
         AND event_rules_digest ~ '^[0-9a-f]{64}$')
    );

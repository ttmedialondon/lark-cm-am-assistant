-- Existing files did not assert CAP status. New uploads explicitly provide it.
ALTER TABLE creator_import_batches ADD COLUMN creator_type TEXT NOT NULL DEFAULT 'pending'
    CHECK (creator_type IN ('cap','non_cap','pending'));
ALTER TABLE creator_import_batches
    DROP CONSTRAINT creator_import_batches_cm_authorization_id_uploaded_by_file_key;
ALTER TABLE creator_import_batches ADD CONSTRAINT creator_import_batch_identity
    UNIQUE(cm_authorization_id,uploaded_by,file_fingerprint,policy_version,creator_type);

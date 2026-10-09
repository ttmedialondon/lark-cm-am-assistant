-- Freeze the material of each paid attempt, independent of credential/config rotation.
-- Nullable for historical requests; their accounting and encrypted diagnostics are retained.
ALTER TABLE creator_handle_requests ADD COLUMN material_fingerprint TEXT;
ALTER TABLE creator_responsibility_requests ADD COLUMN material_fingerprint TEXT;
ALTER TABLE creator_group_requests ADD COLUMN material_fingerprint TEXT;
ALTER TABLE creator_importance_requests ADD COLUMN material_fingerprint TEXT;

-- Rule extraction is intentionally idempotent: a later incremental overlap or
-- manual history rescan must not create another candidate or evidence row.

CREATE UNIQUE INDEX creator_identity_candidates_rule_subject_idx
    ON creator_identity_candidates (subject_open_id, identifier_kind, platform, normalized_value)
    WHERE subject_open_id IS NOT NULL;

CREATE UNIQUE INDEX creator_candidate_evidence_message_idx
    ON creator_candidate_evidence (candidate_id, chat_id, message_id)
    WHERE message_id IS NOT NULL;

-- Admission signals only; analysis still uses the existing authorized summary queue.
-- Do not lock reports inside admission transactions (their source locks run first).
CREATE TABLE offhours_summary_backfills (
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    responsible_authorization_id UUID NOT NULL REFERENCES authorizations(id) ON DELETE CASCADE,
    version BIGINT NOT NULL DEFAULT 1,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contact_id,responsible_authorization_id)
);
CREATE INDEX offhours_summary_backfills_due ON offhours_summary_backfills(not_before,requested_at);

CREATE FUNCTION enqueue_summary_backfill(target UUID, cm UUID DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM contact_roles WHERE contact_id=target
        AND role='creator' AND status='confirmed') THEN RETURN; END IF;
    INSERT INTO offhours_summary_backfills(contact_id,responsible_authorization_id)
    SELECT target,p.responsible_authorization_id FROM (
        SELECT responsible_authorization_id FROM contact_assignments
        WHERE contact_id=target AND ended_at IS NULL
        UNION
        SELECT responsible_authorization_id FROM creator_account_assignments
        WHERE contact_id=target AND ended_at IS NULL AND relationship='owner'
    ) p WHERE cm IS NULL OR p.responsible_authorization_id=cm
    ORDER BY p.responsible_authorization_id
    ON CONFLICT (contact_id,responsible_authorization_id) DO UPDATE SET
        version=offhours_summary_backfills.version+1,requested_at=NOW(),not_before=NOW();
END $$;

CREATE FUNCTION summary_creator_admitted() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role='creator' AND NEW.status='confirmed' AND
        (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status
         OR OLD.role IS DISTINCT FROM NEW.role OR OLD.contact_id IS DISTINCT FROM NEW.contact_id) THEN
        PERFORM enqueue_summary_backfill(NEW.contact_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER summary_creator_admission AFTER INSERT OR UPDATE ON contact_roles
    FOR EACH ROW EXECUTE FUNCTION summary_creator_admitted();

CREATE FUNCTION summary_responsibility_added() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.ended_at IS NOT NULL THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME='creator_account_assignments' THEN
        IF NEW.relationship<>'owner' THEN RETURN NEW; END IF;
        IF TG_OP='UPDATE' AND OLD.relationship IS DISTINCT FROM NEW.relationship THEN
            PERFORM enqueue_summary_backfill(NEW.contact_id,NEW.responsible_authorization_id);
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP='INSERT' OR OLD.ended_at IS DISTINCT FROM NEW.ended_at
        OR OLD.contact_id IS DISTINCT FROM NEW.contact_id
        OR OLD.responsible_authorization_id IS DISTINCT FROM NEW.responsible_authorization_id THEN
        PERFORM enqueue_summary_backfill(NEW.contact_id,NEW.responsible_authorization_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER summary_person_responsibility AFTER INSERT OR UPDATE ON contact_assignments
    FOR EACH ROW EXECUTE FUNCTION summary_responsibility_added();
CREATE TRIGGER summary_account_responsibility AFTER INSERT OR UPDATE ON creator_account_assignments
    FOR EACH ROW EXECUTE FUNCTION summary_responsibility_added();

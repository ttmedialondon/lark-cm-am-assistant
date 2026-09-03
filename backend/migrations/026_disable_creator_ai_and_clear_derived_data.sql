-- Retire the inaccurate creator-AI workflow while preserving source facts for
-- a future manual import. This removes only derived creator classifications,
-- owner matches, and the assessments that could recreate those facts.

LOCK TABLE
    ai_feature_configs,
    contact_assessments,
    contact_assessment_refs,
    contact_assignment_assessments,
    contact_assignment_assessment_refs,
    contact_assignments,
    contact_roles
IN ACCESS EXCLUSIVE MODE;

UPDATE ai_feature_configs
SET enabled = FALSE,
    updated_at = NOW()
WHERE feature IN ('contact_assessment', 'contact_assignment');

-- Stop unrelated classification work too: the whole identity feature is off.
-- Keep each queue watermark unchanged so a later product decision cannot
-- accidentally rescan all historical messages.
UPDATE contact_assessments
SET status = 'cancelled',
    outcome = 'no_action',
    review_status = 'not_required',
    finished_at = NOW(),
    lease_until = NULL,
    error_message = '达人 AI 识别已停用；任务已取消。',
    updated_at = NOW()
WHERE status IN ('queued', 'running');

UPDATE contact_assignment_assessments
SET status = 'cancelled',
    outcome = 'no_action',
    finished_at = NOW(),
    lease_until = NULL,
    error_message = '达人负责人 AI 匹配已停用；任务已取消。',
    updated_at = NOW()
WHERE status IN ('queued', 'running');

CREATE TEMP TABLE creator_cleanup_contacts (
    contact_id UUID PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO creator_cleanup_contacts (contact_id)
SELECT contact_id
FROM contact_roles
WHERE role = 'creator'
UNION
SELECT contact_id FROM contact_assignments
UNION
SELECT contact_id FROM contact_assignment_assessments
UNION
SELECT assessment.contact_id
FROM contact_assessments assessment
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(assessment.result -> 'roles') = 'array'
                THEN assessment.result -> 'roles'
            ELSE '[]'::jsonb
        END
    ) suggestion
    WHERE suggestion ->> 'role' = 'creator'
);

CREATE TEMP TABLE creator_cleanup_counts ON COMMIT DROP AS
SELECT
    (SELECT COUNT(*) FROM creator_cleanup_contacts) AS affected_contact_count,
    (SELECT COUNT(*) FROM contact_roles WHERE role = 'creator') AS creator_role_count,
    (SELECT COUNT(*) FROM contact_assignments) AS assignment_count,
    (SELECT COUNT(*) FROM contact_assignment_assessments) AS assignment_assessment_count,
    (SELECT COUNT(*) FROM contact_assignment_assessment_refs) AS assignment_reference_count,
    (
        SELECT COUNT(*)
        FROM contact_assessments assessment
        JOIN creator_cleanup_contacts affected ON affected.contact_id = assessment.contact_id
    ) AS identity_assessment_count,
    (
        SELECT COUNT(*)
        FROM contact_assessment_refs reference
        JOIN contact_assessments assessment ON assessment.id = reference.assessment_id
        JOIN creator_cleanup_contacts affected ON affected.contact_id = assessment.contact_id
    ) AS identity_reference_count;

DELETE FROM contact_assignments;
DELETE FROM contact_assignment_assessments;

-- Remove every identity assessment for a contact that ever entered the
-- creator workflow, including that contact's no-action history. This is
-- intentional: no latest-result or confirmation path may retain stale AI
-- state for a former creator candidate.
DELETE FROM contact_assessments assessment
USING creator_cleanup_contacts affected
WHERE assessment.contact_id = affected.contact_id;

DELETE FROM contact_roles
WHERE role = 'creator';

INSERT INTO audit_events (id, authorization_id, action, metadata)
SELECT
    gen_random_uuid(),
    NULL,
    'creator_ai_features_disabled_and_data_cleared',
    jsonb_build_object(
        'affected_contact_count', affected_contact_count,
        'creator_role_count', creator_role_count,
        'assignment_count', assignment_count,
        'assignment_assessment_count', assignment_assessment_count,
        'assignment_reference_count', assignment_reference_count,
        'identity_assessment_count', identity_assessment_count,
        'identity_reference_count', identity_reference_count
    )
FROM creator_cleanup_counts;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM ai_feature_configs
        WHERE feature IN ('contact_assessment', 'contact_assignment') AND enabled
    ) THEN
        RAISE EXCEPTION 'creator AI features are still enabled';
    END IF;
    IF EXISTS (SELECT 1 FROM contact_roles WHERE role = 'creator') THEN
        RAISE EXCEPTION 'creator roles remain after cleanup';
    END IF;
    IF EXISTS (SELECT 1 FROM contact_assignments) THEN
        RAISE EXCEPTION 'creator assignments remain after cleanup';
    END IF;
    IF EXISTS (SELECT 1 FROM contact_assignment_assessments)
       OR EXISTS (SELECT 1 FROM contact_assignment_assessment_refs) THEN
        RAISE EXCEPTION 'creator assignment assessments remain after cleanup';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM contact_assessments assessment
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(assessment.result -> 'roles') = 'array'
                        THEN assessment.result -> 'roles'
                    ELSE '[]'::jsonb
                END
            ) suggestion
            WHERE suggestion ->> 'role' = 'creator'
        )
    ) THEN
        RAISE EXCEPTION 'creator identity results remain after cleanup';
    END IF;
    IF EXISTS (SELECT 1 FROM contact_assessments WHERE status IN ('queued', 'running')) THEN
        RAISE EXCEPTION 'contact identity work remains active after shutdown';
    END IF;
END $$;

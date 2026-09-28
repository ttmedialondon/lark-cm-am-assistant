"""Batch confirmed TikTok handles for a displayed page of people."""


def for_contacts(conn, contact_ids, cm_id=None):
    result = {contact_id: [] for contact_id in contact_ids}
    if not result:
        return result
    rows = conn.execute("""WITH links AS (
        SELECT contact_id,social_account_id FROM contact_social_accounts
        WHERE contact_id=ANY(%s::uuid[]) AND status='confirmed' AND relationship='owner'
        UNION SELECT contact_id,social_account_id FROM creator_account_assignments
        WHERE contact_id=ANY(%s::uuid[]) AND ended_at IS NULL
          AND (%s::uuid IS NULL OR responsible_authorization_id=%s::uuid)
    ) SELECT links.contact_id,s.display_handle FROM links
      JOIN social_accounts s ON s.id=links.social_account_id
      WHERE s.platform='tiktok' ORDER BY s.normalized_handle""",
        (contact_ids, contact_ids, cm_id, cm_id)).fetchall()
    for row in rows:
        result[row['contact_id']].append(row['display_handle'])
    return result

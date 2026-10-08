"""Batch confirmed TikTok handles for a displayed page of people."""

from uuid import uuid4

from . import creator_access, creator_data
from .handle_evidence import normalize_handle


def confirmed_pairs(conn, contact_ids):
    """Formal owners; only explicitly reviewed human sources are reusable human facts."""
    if not contact_ids:
        return []
    return conn.execute("""SELECT l.contact_id,lower(s.normalized_handle) handle,l.source,
        (l.source IN ('cm_confirmation','owner_reviewed_curva')
         AND l.reviewed_by_authorization_id IS NOT NULL AND l.reviewed_at IS NOT NULL) human_confirmed
        FROM contact_social_accounts l JOIN social_accounts s ON s.id=l.social_account_id
        WHERE l.contact_id=ANY(%s::uuid[]) AND l.relationship='owner' AND l.status='confirmed'
          AND s.platform='tiktok' ORDER BY l.contact_id,s.normalized_handle""", (contact_ids,)).fetchall()


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


def set_manual(conn, actor_id, contact_id, cm_id, old_handle, handle):
    """Replace one global account fact, keeping sibling handles and CM history."""
    from . import creator_imports

    actor_id = creator_access.parse_id(actor_id)
    contact_id = creator_access.parse_id(contact_id)
    creator_access.require_owner(conn, actor_id)
    cm_id, _ = creator_access.access(conn, actor_id, cm_id, read_only=True)
    try:
        old_handle = normalize_handle(old_handle) if old_handle.strip() else ""
        handle = normalize_handle(handle)
    except (ValueError, AttributeError) as exc:
        raise ValueError("请填写正确的 handle。") from exc
    for value in sorted({old_handle, handle} - {""}):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + value,))
    if not conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (contact_id,)).fetchone():
        raise LookupError("找不到该达人。")
    # Locks can wait behind another correction; check the current scope and facts again.
    creator_access.require_owner(conn, actor_id)
    creator_access.access(conn, actor_id, cm_id, read_only=True)
    if not conn.execute("SELECT creator_importance_is_eligible(%s,%s) ok", (contact_id, cm_id)).fetchone()["ok"]:
        raise PermissionError("该达人不在此负责人的有效名单中。")
    current = conn.execute("""SELECT s.id,lower(s.normalized_handle) handle FROM social_accounts s
        WHERE s.platform='tiktok' AND (
          EXISTS(SELECT 1 FROM contact_social_accounts l WHERE l.social_account_id=s.id
            AND l.contact_id=%s AND l.relationship='owner' AND l.status='confirmed')
          OR EXISTS(SELECT 1 FROM creator_account_assignments a WHERE a.social_account_id=s.id
            AND a.contact_id=%s AND a.ended_at IS NULL)) ORDER BY s.id""", (contact_id, contact_id)).fetchall()
    before = {row["handle"] for row in current}
    old_accounts = [row["id"] for row in current if row["handle"] == old_handle]
    if (old_handle and not old_accounts) or (not old_handle and before):
        raise ValueError("该达人的 handle 已变化，请刷新后重试。")
    if len(old_accounts) > 1 or creator_imports._identity_conflict(conn, contact_id):
        raise ValueError(creator_imports.REASONS["account_conflict"])
    if old_handle == handle:
        return {"saved": True, "handles": for_contacts(conn, [contact_id], cm_id)[contact_id]}
    accounts = conn.execute("SELECT id FROM social_accounts WHERE platform='tiktok' "
                            "AND lower(normalized_handle)=%s", (handle,)).fetchall()
    if len(accounts) > 1:
        raise ValueError(creator_imports.REASONS["account_conflict"])
    account_id = accounts[0]["id"] if accounts else None
    if account_id and conn.execute("""SELECT 1 FROM contact_social_accounts WHERE social_account_id=%s
        AND (status='confirmed' AND (contact_id<>%s OR relationship<>'owner')
          OR contact_id=%s AND relationship<>'owner')
        UNION ALL SELECT 1 FROM creator_account_assignments WHERE social_account_id=%s
          AND ended_at IS NULL AND (contact_id<>%s OR relationship<>'owner') LIMIT 1""",
        (account_id, contact_id, contact_id, account_id, contact_id)).fetchone():
        raise ValueError(creator_imports.REASONS["account_conflict"])
    if not account_id:
        account_id = conn.execute("""INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
            VALUES(%s,'tiktok',%s,%s) RETURNING id""", (uuid4(), handle, handle)).fetchone()["id"]
    conn.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,
        source,reviewed_by_authorization_id,reviewed_at)
        VALUES(%s,%s,'owner','confirmed','cm_confirmation',%s,NOW())
        ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET status='confirmed',
          source='cm_confirmation',reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,
          reviewed_at=NOW(),updated_at=NOW()""", (contact_id, account_id, actor_id))
    retired = []
    if old_accounts:
        old_account_id = old_accounts[0]
        # Retain a rejection even for legacy assignment-only handles, so workers cannot restore it.
        conn.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,
            source,reviewed_by_authorization_id,reviewed_at)
            VALUES(%s,%s,'owner','rejected','cm_confirmation',%s,NOW())
            ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET status='rejected',
              source='cm_confirmation',reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,
              reviewed_at=NOW(),updated_at=NOW()""", (contact_id, old_account_id, actor_id))
        conn.execute("""INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,
            created_by_authorization_id,started_at)
            SELECT gen_random_uuid(),contact_id,responsible_authorization_id,
              CASE WHEN source IN ('excel_ai_verified','ai_business_verified') THEN 'ai' ELSE 'manual' END,
              created_by_authorization_id,started_at FROM creator_account_assignments
            WHERE contact_id=%s AND social_account_id=%s AND ended_at IS NULL
            ON CONFLICT(contact_id,responsible_authorization_id) WHERE ended_at IS NULL DO NOTHING""",
            (contact_id, old_account_id))
        retired = conn.execute("""UPDATE creator_account_assignments SET ended_at=NOW()
            WHERE contact_id=%s AND social_account_id=%s AND ended_at IS NULL RETURNING id""",
            (contact_id, old_account_id)).fetchall()
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor_id, "creator_handle_manual", creator_data.encode({
            "contact_id": str(contact_id), "cm_id": str(cm_id), "before": sorted(before),
            "after": sorted((before - {old_handle}) | {handle}),
            "retired_account_assignment_ids": [str(row["id"]) for row in retired],
        })))
    return {"saved": True, "handles": for_contacts(conn, [contact_id], cm_id)[contact_id]}

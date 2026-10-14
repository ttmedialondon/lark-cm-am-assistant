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


def for_contacts(conn, contact_ids):
    result = {contact_id: [] for contact_id in contact_ids}
    if not result:
        return result
    rows = conn.execute("""SELECT links.contact_id,s.display_handle FROM contact_social_accounts links
        JOIN social_accounts s ON s.id=links.social_account_id
        WHERE links.contact_id=ANY(%s::uuid[]) AND links.status='confirmed' AND links.relationship='owner'
          AND s.platform='tiktok' ORDER BY s.normalized_handle""", (contact_ids,)).fetchall()
    for row in rows:
        result[row['contact_id']].append(row['display_handle'])
    return result


def _manual_scope(conn, actor_id, cm_id, *, lock=False):
    actor_id = creator_access.parse_id(actor_id)
    cm_id = creator_access.parse_id(cm_id) if cm_id else actor_id
    if lock:
        ids = sorted({actor_id, cm_id}, key=str)
        conn.execute("SELECT id FROM authorizations WHERE id=ANY(%s::uuid[]) ORDER BY id FOR SHARE",
                     (ids,)).fetchall()
        conn.execute("SELECT authorization_id FROM admin_roles WHERE authorization_id=ANY(%s::uuid[]) "
                     "ORDER BY authorization_id FOR SHARE", (ids,)).fetchall()
    owner = creator_access.is_owner(conn, actor_id)
    if actor_id != cm_id and not owner:
        raise PermissionError("只能维护您自己的达人。")
    if not creator_access.authorized_cm(conn, cm_id):
        raise PermissionError("该负责人的聊天授权不可用，请先完成授权。")
    return cm_id, owner


def search_people(conn, actor_id, cm_id, query):
    """Names and context only, bounded to the selected CM's visible contacts."""
    from . import creator_imports
    from .identity_profiles import safe_avatar_url

    cm_id, _ = _manual_scope(conn, actor_id, cm_id)
    query = str(query or "").strip()[:120]
    if not query:
        return {"cm_id": str(cm_id), "items": []}
    rows = conn.execute("""WITH visible AS MATERIALIZED (
        SELECT c.id,c.display_name FROM contacts c WHERE (
          POSITION(lower(%s) IN lower(COALESCE(c.display_name,'')))>0
          OR EXISTS(SELECT 1 FROM lark_identities i WHERE i.contact_id=c.id
            AND POSITION(lower(%s) IN lower(COALESCE(i.display_name,'')))>0))
          AND EXISTS (""" + creator_imports._ACCESSIBLE + """)
          AND NOT EXISTS(SELECT 1 FROM lark_identities i WHERE i.contact_id=c.id
            AND i.identity_type='user' AND i.tenant_classification='internal')
        ORDER BY c.display_name,c.id LIMIT 20)
        SELECT c.id,c.display_name,identity.open_id,identity.avatar_url,
        (SELECT COALESCE(NULLIF(ch.name,''),'相关会话') FROM authorization_chats ac
          JOIN chats ch ON ch.chat_id=ac.chat_id WHERE ac.authorization_id=%s
            AND ac.is_visible AND ac.sync_enabled AND COALESCE(ch.chat_status,'normal')='normal'
            AND (ac.p2p_counterpart_open_id=identity.open_id OR EXISTS(SELECT 1 FROM chat_members m
                WHERE m.chat_id=ac.chat_id AND m.open_id=identity.open_id))
          ORDER BY ch.chat_mode DESC,ch.chat_id LIMIT 1) context_label
        FROM visible c JOIN LATERAL (
          SELECT i.open_id,CASE WHEN i.avatar_status='unavailable' THEN NULL ELSE i.avatar_url END avatar_url
          FROM lark_identities i JOIN authorizations a ON a.id=%s
          WHERE i.contact_id=c.id AND i.identity_type='user' AND i.open_id<>a.open_id AND (
            EXISTS(SELECT 1 FROM authorization_chats ac JOIN chats ch ON ch.chat_id=ac.chat_id
              WHERE ac.authorization_id=a.id AND ac.is_visible AND ac.sync_enabled
              AND COALESCE(ch.chat_status,'normal')='normal' AND ac.p2p_counterpart_open_id=i.open_id)
            OR EXISTS(SELECT 1 FROM chat_members m JOIN authorization_chats ac ON ac.chat_id=m.chat_id
              JOIN chats ch ON ch.chat_id=ac.chat_id WHERE m.open_id=i.open_id AND ac.authorization_id=a.id
              AND ac.is_visible AND ac.sync_enabled AND COALESCE(ch.chat_status,'normal')='normal')
            OR EXISTS(SELECT 1 FROM chat_messages m JOIN authorization_message_observations o
              ON o.chat_id=m.chat_id AND o.message_id=m.message_id AND o.authorization_id=a.id
              JOIN authorization_chats ac ON ac.chat_id=o.chat_id AND ac.authorization_id=a.id
              JOIN chats ch ON ch.chat_id=ac.chat_id WHERE m.sender_open_id=i.open_id
              AND o.observed_at>=a.body_consent_at AND NOT m.is_deleted AND m.body_ciphertext IS NOT NULL
              AND ac.is_visible AND ac.sync_enabled AND COALESCE(ch.chat_status,'normal')='normal'))
          ORDER BY i.last_seen_at DESC,i.open_id LIMIT 1) identity ON TRUE
        ORDER BY c.display_name,c.id""", (query, query, cm_id, cm_id, cm_id)).fetchall()
    handles = for_contacts(conn, [row["id"] for row in rows])
    return {"cm_id": str(cm_id), "items": [{"contact_id": str(row["id"]),
        "display_name": row["display_name"] or "未命名用户", "open_id": row["open_id"],
        "avatar_url": safe_avatar_url(row["avatar_url"]),
        "context_label": row["context_label"] or "有历史交流", "handles": handles[row["id"]]} for row in rows]}


def _account(conn, handle):
    accounts = conn.execute("SELECT id FROM social_accounts WHERE platform='tiktok' "
                            "AND lower(normalized_handle)=%s", (handle,)).fetchall()
    if len(accounts) > 1:
        raise ValueError("账号存在重复记录，请联系管理员。")
    return accounts[0]["id"] if accounts else None


def _conflict(conn, account_id, contact_id, actor_id, owner):
    if not account_id:
        return
    if conn.execute("""SELECT 1 FROM contact_social_accounts WHERE social_account_id=%s
        AND (status='confirmed' AND (contact_id<>%s OR relationship<>'owner')
          OR contact_id=%s AND relationship<>'owner')
        LIMIT 1""", (account_id, contact_id, contact_id)).fetchone():
        raise ValueError("此账号已关联其他人员或存在身份冲突，未覆盖原资料。")
    if not owner and conn.execute("""SELECT 1 FROM contact_social_accounts WHERE social_account_id=%s
        AND contact_id=%s AND relationship='owner' AND status='rejected'
        AND reviewed_by_authorization_id IS DISTINCT FROM %s""",
        (account_id, contact_id, actor_id)).fetchone():
        raise PermissionError("此账号曾被其他人员排除，请由 Owner 核对。")


def _confirm(conn, contact_id, account_id, handle, actor_id):
    if not account_id:
        account_id = conn.execute("""INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
            VALUES(%s,'tiktok',%s,%s) RETURNING id""", (uuid4(), handle, handle)).fetchone()["id"]
    conn.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,
        source,reviewed_by_authorization_id,reviewed_at)
        VALUES(%s,%s,'owner','confirmed','cm_confirmation',%s,NOW())
        ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET status='confirmed',
          source=CASE WHEN contact_social_accounts.status='confirmed'
            AND contact_social_accounts.reviewed_by_authorization_id IS NOT NULL
            THEN contact_social_accounts.source ELSE 'cm_confirmation' END,
          reviewed_by_authorization_id=CASE WHEN contact_social_accounts.status='confirmed'
            AND contact_social_accounts.reviewed_by_authorization_id IS NOT NULL
            THEN contact_social_accounts.reviewed_by_authorization_id ELSE EXCLUDED.reviewed_by_authorization_id END,
          reviewed_at=CASE WHEN contact_social_accounts.status='confirmed'
            AND contact_social_accounts.reviewed_by_authorization_id IS NOT NULL
            THEN contact_social_accounts.reviewed_at ELSE NOW() END,updated_at=NOW()""",
        (contact_id, account_id, actor_id))
    return account_id


def _reject(conn, contact_id, account_id, actor_id):
    conn.execute("""INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,
        source,reviewed_by_authorization_id,reviewed_at)
        VALUES(%s,%s,'owner','rejected','cm_confirmation',%s,NOW())
        ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET status='rejected',
          source='cm_confirmation',reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,
          reviewed_at=NOW(),updated_at=NOW()""", (contact_id, account_id, actor_id))


def set_manual(conn, actor_id, contact_id, cm_id, old_handle, handle, *, operation="replace"):
    """Change one account fact, keeping sibling handles and every CM responsibility."""
    from . import creator_imports

    actor_id = creator_access.parse_id(actor_id)
    contact_id = creator_access.parse_id(contact_id)
    cm_id, owner = _manual_scope(conn, actor_id, cm_id, lock=True)
    if operation not in {"add", "replace", "remove"}:
        raise ValueError("账号操作无效。")
    try:
        old_handle = normalize_handle(old_handle) if old_handle.strip() else ""
        handle = normalize_handle(handle) if operation != "remove" else ""
    except (ValueError, AttributeError) as exc:
        raise ValueError("请填写正确的 handle。") from exc
    if (operation == "add" and old_handle) or (operation == "remove" and not old_handle):
        raise ValueError("请选择要修改的账号。")
    for value in sorted({old_handle, handle} - {""}):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + value,))
    if not conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (contact_id,)).fetchone():
        raise LookupError("找不到该达人。")
    # Locks can wait behind another correction; check the current scope and facts again.
    cm_id, owner = _manual_scope(conn, actor_id, cm_id, lock=True)
    if not conn.execute("SELECT creator_importance_is_eligible(%s,%s) ok", (contact_id, cm_id)).fetchone()["ok"]:
        raise PermissionError("该达人不在此负责人的有效名单中。")
    current = conn.execute("""SELECT s.id,lower(s.normalized_handle) handle FROM social_accounts s
        JOIN contact_social_accounts l ON l.social_account_id=s.id
        WHERE s.platform='tiktok' AND l.contact_id=%s AND l.relationship='owner' AND l.status='confirmed'
        ORDER BY s.id""", (contact_id,)).fetchall()
    before = {row["handle"] for row in current}
    old_accounts = [row["id"] for row in current if row["handle"] == old_handle]
    if (old_handle and not old_accounts) or (operation == "replace" and not old_handle and before):
        raise ValueError("该达人的 handle 已变化，请刷新后重试。")
    if len(old_accounts) > 1 or creator_imports._identity_conflict(conn, contact_id):
        raise ValueError(creator_imports.REASONS["account_conflict"])
    if old_handle == handle:
        return {"saved": True, "handles": for_contacts(conn, [contact_id])[contact_id]}
    if handle:
        account_id = _account(conn, handle)
        _conflict(conn, account_id, contact_id, actor_id, owner)
        _confirm(conn, contact_id, account_id, handle, actor_id)
    if old_accounts:
        _reject(conn, contact_id, old_accounts[0], actor_id)
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor_id, "creator_handle_manual", creator_data.encode({
            "contact_id": str(contact_id), "cm_id": str(cm_id), "before": sorted(before),
            "after": sorted((before - {old_handle}) | ({handle} if handle else set())), "operation": operation,
        })))
    return {"saved": True, "handles": for_contacts(conn, [contact_id])[contact_id]}


def link_manual(conn, actor_id, cm_id, handle, contact_id, *, old_contact_id=None):
    """Confirm a visible person; an explicit Owner correction only moves the account fact."""
    from . import creator_imports

    actor_id = creator_access.parse_id(actor_id)
    contact_id = creator_access.parse_id(contact_id)
    old_contact_id = creator_access.parse_id(old_contact_id) if old_contact_id else None
    cm_id, owner = _manual_scope(conn, actor_id, cm_id, lock=True)
    if old_contact_id and not owner:
        raise PermissionError("只有 Owner 可以更正账号对应的 Lark 用户。")
    if old_contact_id == contact_id:
        raise ValueError("请选择另一位 Lark 用户；同一用户请直接管理账号。")
    try:
        handle = normalize_handle(handle)
    except (ValueError, AttributeError) as exc:
        raise ValueError("请填写正确的 handle。") from exc
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + handle,))
    people = sorted({contact_id, *([old_contact_id] if old_contact_id else [])}, key=str)
    locked = conn.execute("SELECT id FROM contacts WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE",
                          (people,)).fetchall()
    if len(locked) != len(people):
        raise LookupError("找不到对应的 Lark 用户。")
    cm_id, owner = _manual_scope(conn, actor_id, cm_id)
    if old_contact_id and not owner:
        raise PermissionError("只有 Owner 可以更正账号对应的 Lark 用户。")
    if not creator_imports._accessible(conn, cm_id, contact_id):
        raise PermissionError("该用户不在当前负责人的可见联系人中。")
    if creator_imports._identity_conflict(conn, contact_id):
        raise ValueError("人员身份存在冲突，未覆盖已确认资料。")
    account_id = _account(conn, handle)
    if old_contact_id:
        owners = conn.execute("""SELECT contact_id FROM contact_social_accounts WHERE social_account_id=%s
            AND relationship='owner' AND status='confirmed'""", (account_id,)).fetchall()
        if {row["contact_id"] for row in owners} != {old_contact_id}:
            raise ValueError("原账号关联已变化，请刷新后重新核对。")
        _reject(conn, old_contact_id, account_id, actor_id)
    _conflict(conn, account_id, contact_id, actor_id, owner)
    _confirm(conn, contact_id, account_id, handle, actor_id)
    if not conn.execute("SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' "
                        "AND status='confirmed'", (contact_id,)).fetchone():
        creator_imports._record_roster_identity(conn, actor_id, contact_id)
    responsibility_added = False
    if not old_contact_id:
        responsibility_added = conn.execute("""INSERT INTO contact_assignments(
            id,contact_id,responsible_authorization_id,source,created_by_authorization_id)
            VALUES(%s,%s,%s,'manual',%s)
            ON CONFLICT(contact_id,responsible_authorization_id) WHERE ended_at IS NULL DO NOTHING RETURNING id""",
            (uuid4(), contact_id, cm_id, actor_id)).fetchone() is not None
    conn.execute("INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor_id, "creator_handle_manual_link", creator_data.encode({"handle": handle,
            "contact_id": str(contact_id), "cm_id": str(cm_id),
            "old_contact_id": str(old_contact_id) if old_contact_id else None,
            "responsibility_added": responsibility_added})))
    return {"saved": True, "contact_id": str(contact_id), "correction": old_contact_id is not None,
            "handles": for_contacts(conn, [contact_id])[contact_id]}

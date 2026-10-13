"""Confirmed creator rosters, independent of retired candidate pages and scanners."""

from . import admin_permissions, contact_handles, creator_access, creator_importance, creator_imports
from .config import get_settings
from .db import connection
from .identity_profiles import safe_avatar_url
from .lark_api import private_chat_url


def page_data(actor_id, cm_id=None, tab="", query="", page=1, sort="importance", tier=""):
    """Confirmed people, with an Owner-only unassigned view."""
    query = str(query or "").strip()[:120]
    page = max(1, min(int(page), 10000))
    with connection() as conn, conn.transaction():
        can_view_unassigned = creator_access.is_owner(conn, actor_id)
        tab = tab if tab in {"mine", "unassigned"} else "mine"
        sort = sort if sort in {"name", "importance"} and tab == "mine" else "name"
        tier = tier if tier in creator_importance.TIERS and tab == "mine" else ""
        unassigned = tab == 'unassigned'
        if unassigned:
            creator_access.require_owner(conn, actor_id)
        cm_id, can_manage = creator_access.access(conn, actor_id, cm_id, read_only=True)
        can_select_cm = creator_access.can_view_all(conn, actor_id)
        read_only = unassigned or (not can_manage and creator_access.parse_id(actor_id) != cm_id)
        can_view_importance = tab == "mine" and (can_manage or creator_access.parse_id(actor_id) == cm_id)
        if not can_view_importance:
            sort, tier = "name", ""
        cms = [
            {"id": str(r["id"]), "name": r["display_name"] or "未命名用户"}
            for r in conn.execute(
                """SELECT id,display_name FROM authorizations WHERE status='active'
                AND body_consent_at IS NOT NULL ORDER BY display_name,id"""
            ).fetchall()
            if can_select_cm or r["id"] == cm_id
        ]
        import_batches = [] if read_only else creator_imports.batch_summaries(conn, cm_id)
        # Person-level and older account-level assignments describe the same
        # library. UNION ensures that a person with several handles appears once.
        formal_base = """FROM contacts c WHERE EXISTS(SELECT 1 FROM contact_roles r
            WHERE r.contact_id=c.id AND r.role='creator' AND r.status='confirmed') AND c.id IN (
            SELECT contact_id FROM contact_assignments
            WHERE responsible_authorization_id=%s AND ended_at IS NULL
            UNION SELECT contact_id FROM creator_account_assignments
            WHERE responsible_authorization_id=%s AND ended_at IS NULL)"""
        args = [cm_id, cm_id]
        if unassigned:
            formal_base = """FROM contacts c WHERE EXISTS(SELECT 1 FROM contact_roles r
                WHERE r.contact_id=c.id AND r.role='creator' AND r.status='confirmed')
                AND NOT EXISTS(SELECT 1 FROM contact_assignments a WHERE a.contact_id=c.id AND a.ended_at IS NULL)
                AND NOT EXISTS(SELECT 1 FROM creator_account_assignments a
                    WHERE a.contact_id=c.id AND a.ended_at IS NULL)"""
            args = []
        confirmed = (conn.execute("SELECT count(*) n " + formal_base, args).fetchone()["n"]
                     if not query and (not tier or not can_view_importance) else None)
        can_view_profiles = "contacts" in admin_permissions.permission_keys_for(actor_id)
        order = "c.display_name,c.id"
        if can_view_importance:
            importance_join, join_args = creator_importance.roster_sql(cm_id)
            formal_base = formal_base.replace(
                "FROM contacts c WHERE", f"FROM contacts c {importance_join} WHERE", 1,
            )
            args = [*join_args, *args]
            if tier:
                formal_base += " AND COALESCE(ip.effective_tier,'pending')=%s"
                args.append(tier)
            if sort == "importance":
                order = """CASE COALESCE(ip.effective_tier,'pending') WHEN 'focus' THEN 0
                    WHEN 'priority' THEN 1 WHEN 'normal' THEN 2 WHEN 'dormant' THEN 3 ELSE 4 END,
                    ip.recency_rank,ip.business_days DESC,
                    ip.last_substantive_progress_at DESC NULLS LAST,ip.has_history DESC,
                    lower(c.display_name),c.id"""
        if query:
            formal_base += """ AND (POSITION(lower(%s) IN lower(COALESCE(c.display_name,'')))>0
                OR EXISTS (SELECT 1 FROM contact_social_accounts l JOIN social_accounts s
                    ON s.id=l.social_account_id WHERE l.contact_id=c.id AND l.status='confirmed'
                    AND l.relationship='owner' AND POSITION(lower(%s) IN lower(s.normalized_handle))>0))"""
            args.extend((query,query))
        total = (conn.execute("SELECT count(*) n " + formal_base, args).fetchone()["n"]
                 if query or tier else confirmed)
        pages = max(1,(total+24)//25)
        page = min(page,pages)
        fields = "c.id,c.display_name"
        if can_view_importance:
            fields += ",COALESCE(ip.effective_tier,'pending') effective_tier,ip.manual_tier,ip.manual_reason_ciphertext"
        people = conn.execute("SELECT " + fields + " " + formal_base
                              + f" ORDER BY {order} LIMIT 25 OFFSET %s", [*args,(page-1)*25]).fetchall()
        ids = [person["id"] for person in people]
        identities = {r["contact_id"]: r for r in conn.execute(
            """SELECT DISTINCT ON (contact_id) contact_id,open_id,
                CASE WHEN avatar_status='unavailable' THEN NULL ELSE avatar_url END avatar_url
                FROM lark_identities WHERE contact_id=ANY(%s::uuid[]) AND identity_type='user'
                ORDER BY contact_id,last_seen_at DESC,open_id""", (ids,),
        ).fetchall()}
        handles = contact_handles.for_contacts(conn, ids, cm_id)
        items = []
        for person in people:
            identity = identities.get(person["id"])
            chat_url = private_chat_url(identity["open_id"] if identity else None)
            importance = None
            if can_view_importance:
                importance = {
                    "tier": person["effective_tier"],
                    "tier_label": creator_importance.TIERS[person["effective_tier"]],
                    "manual_tier": person["manual_tier"] or "",
                    "manual_reason": (creator_importance._cipher(get_settings()).decrypt(
                        person["manual_reason_ciphertext"],
                    ) if can_view_unassigned and person["manual_tier"] and person["manual_reason_ciphertext"] else ""),
                }
            items.append({
                "id": str(person["id"]), "kind": "confirmed", "contact_id": str(person["id"]),
                "display_name": person["display_name"] or "未命名用户",
                "open_id": identity["open_id"] if identity else None,
                "avatar_url": safe_avatar_url(identity["avatar_url"]) if identity else None,
                "private_chat_url": chat_url,
                "profile_url": f"/admin/contacts/{identity['open_id']}" if can_view_profiles and chat_url else None,
                "handles": handles[person["id"]],
                "importance": importance,
            })
        return {
            "cms": cms, "selected_cm_id": str(cm_id), "can_manage": can_manage, "tab": tab,
            "can_select_cm": can_select_cm, "read_only": read_only,
            "can_view_unassigned": can_view_unassigned,
            "can_view_importance": can_view_importance,
            "can_adjust_importance": can_view_unassigned and can_view_importance, "sort": sort, "tier": tier,
            "can_edit_handles": tab == "mine" and (can_view_unassigned or creator_access.parse_id(actor_id) == cm_id),
            "can_link_people": tab == "mine" and (can_view_unassigned or creator_access.parse_id(actor_id) == cm_id),
            "importance_running": bool(can_view_importance and conn.execute(
                """SELECT 1 FROM creator_importance_jobs j JOIN ai_feature_configs f
                    ON f.feature='creator_importance' AND f.enabled
                    WHERE j.responsible_authorization_id=%s
                    AND j.status IN ('queued','preparing','ready','running','retry_wait') LIMIT 1""", (cm_id,),
            ).fetchone()),
            "import_batches": import_batches, "items": items,
            "total_count": total, "total_pages": pages, "page": page,
        }

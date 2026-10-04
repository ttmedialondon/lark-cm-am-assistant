"""Current creator roster and import candidates, independent of the retired handle scanner."""

from . import contact_handles, creator_access, creator_imports
from .db import connection
from .identity_profiles import safe_avatar_url
from .lark_api import private_chat_url


def page_data(actor_id, cm_id=None, tab="mine", query="", page=1):
    """Current library: Excel import candidates and confirmed people."""
    query = str(query or "").strip()[:120]
    page = max(1, min(int(page), 10000))
    with connection() as conn:
        unassigned = tab == 'unassigned'
        if unassigned:
            creator_access.require_owner(conn, actor_id)
        cm_id, can_manage = creator_access.access(conn, actor_id, cm_id, read_only=tab != "candidates")
        can_view_unassigned = creator_access.is_owner(conn, actor_id)
        can_select_cm = creator_access.can_view_all(conn, actor_id)
        read_only = unassigned or (not can_manage and creator_access.parse_id(actor_id) != cm_id)
        cms = [
            {"id": str(r["id"]), "name": r["display_name"] or "未命名用户"}
            for r in conn.execute(
                """SELECT id,display_name FROM authorizations WHERE status='active'
                AND body_consent_at IS NOT NULL ORDER BY display_name,id"""
            ).fetchall()
            if can_select_cm or r["id"] == cm_id
        ]
        imported = ({"total": 0, "batches": []} if read_only else
                    creator_imports._page_data(conn, cm_id, q=query if tab=="candidates" else "", page=page,
                                               include_candidates=tab == "candidates"))
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
        confirmed = conn.execute("SELECT count(*) n " + formal_base, args).fetchone()["n"]
        counts = dict.fromkeys(("pending", "preparing", "ready", "queued", "running", "succeeded", "skipped",
                                "failed", "invalid", "total", "completed", "candidates", "confirmed"), 0)
        counts.update(candidates=imported["total"], confirmed=confirmed)
        if tab == "candidates":
            items, total = imported["candidates"], imported["total"]
            pages, page = imported["total_pages"], imported["page"]
        else:
            if query:
                formal_base += """ AND (POSITION(lower(%s) IN lower(COALESCE(c.display_name,'')))>0
                    OR EXISTS (SELECT 1 FROM contact_social_accounts l JOIN social_accounts s
                        ON s.id=l.social_account_id WHERE l.contact_id=c.id AND l.status='confirmed'
                        AND l.relationship='owner' AND POSITION(lower(%s) IN lower(s.normalized_handle))>0))"""
                args.extend((query,query))
            total = conn.execute("SELECT count(*) n " + formal_base, args).fetchone()["n"] if query else confirmed
            pages = max(1,(total+24)//25)
            page = min(page,pages)
            people = conn.execute("SELECT c.id,c.display_name " + formal_base
                                  + " ORDER BY c.display_name,c.id LIMIT 25 OFFSET %s", [*args,(page-1)*25]).fetchall()
            imports_by_person = {person["id"]: [] for person in people}
            for row in conn.execute(
                """SELECT r.id,r.contact_id,r.handle,r.api_requests,r.stats,
                    r.pack_ciphertext IS NOT NULL AS has_evidence
                    FROM creator_import_rows r JOIN creator_import_batches b ON b.id=r.batch_id
                    WHERE r.contact_id=ANY(%s::uuid[]) AND r.cm_authorization_id=%s
                    AND r.review_status='confirmed' AND b.policy_version=%s
                    ORDER BY r.created_at DESC,r.id DESC""",
                (list(imports_by_person), cm_id, creator_imports.POLICY_VERSION),
            ).fetchall():
                imports_by_person[row["contact_id"]].append(row)
            ids = list(imports_by_person)
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
                import_rows = imports_by_person[person["id"]]
                sources = list(dict.fromkeys(
                    label for row in import_rows for label in creator_imports.source_labels(row)
                )) or ["手动"]
                evidence_rows = {}
                for row in import_rows:
                    if row["has_evidence"]:
                        evidence_rows.setdefault(row["handle"], {"id": str(row["id"]), "handle": row["handle"]})
                items.append({
                    "id": str(person["id"]), "kind": "confirmed", "contact_id": str(person["id"]),
                    "display_name": person["display_name"] or "未命名用户",
                    "open_id": identity["open_id"] if identity else None,
                    "avatar_url": safe_avatar_url(identity["avatar_url"]) if identity else None,
                    "private_chat_url": private_chat_url(identity["open_id"] if identity else None),
                    "handles": handles[person["id"]],
                    "sources": sources, "source": " + ".join(sources),
                    "evidence_rows": [] if read_only else list(evidence_rows.values()),
                    "status_label": "已入库", "can_confirm": False, "can_defer": False,
                })
        return {
            "cms": cms, "selected_cm_id": str(cm_id), "can_manage": can_manage,
            "can_select_cm": can_select_cm, "read_only": read_only,
            "can_view_unassigned": can_view_unassigned,
            "run": None, "counts": counts, "usage": {"api_requests": 0, "input_tokens": 0, "output_tokens": 0},
            "import_batches": imported["batches"], "items": items,
            "total_count": total, "total_pages": pages, "page": page,
        }

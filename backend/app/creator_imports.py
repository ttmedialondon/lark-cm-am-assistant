"""Explicit single-CM CAP rosters and manual account-to-person confirmation."""

from __future__ import annotations

import hashlib
import math
from pathlib import PurePath
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import creator_handles
from . import creator_ownership as ownership
from .creator_handle_evidence import normalize_handle
from .creator_import_xlsx import MAX_FILE_BYTES, parse_xlsx  # noqa: F401 - routes share the upload bound
from .db import connection

POLICY_VERSION = "creator-import-cap-manual-v1"
REASONS = {
    "existing": "已有相同账号和负责人，已复用正式关联",
    "manual_review": "请核对 handle，选择对应的 Lark 用户后确认入库。",
    "manual_confirmed": "已确认入库",
    "account_conflict": "存在账号、人员分类、CAP 属性或负责人冲突，未覆盖原资料",
}
BATCH_LABELS = {"preview": "待确认导入", "completed": "已导入"}


def source_labels(row):
    return ["Excel"]


def _audit(conn, actor, action, data):
    conn.execute(
        "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s)",
        (uuid4(), actor, "creator_import_" + action, Jsonb(data)),
    )


def _batch(conn, actor, batch_id):
    batch = conn.execute(
        "SELECT * FROM creator_import_batches WHERE id=%s FOR UPDATE", (ownership._uuid(batch_id),)
    ).fetchone()
    if not batch:
        raise LookupError("找不到该导入批次。")
    creator_handles._access(conn, actor, batch["cm_authorization_id"])
    if batch["policy_version"] != POLICY_VERSION:
        raise ValueError("导入规则已更新，请重新上传名单。")
    return batch


def _row(conn, actor, row_id):
    row = conn.execute("SELECT batch_id FROM creator_import_rows WHERE id=%s", (ownership._uuid(row_id),)).fetchone()
    if not row:
        raise LookupError("找不到该导入候选。")
    batch = _batch(conn, actor, row["batch_id"])
    row = conn.execute(
        "SELECT * FROM creator_import_rows WHERE id=%s FOR UPDATE", (ownership._uuid(row_id),)
    ).fetchone()
    row["batch_creator_type"] = batch["creator_type"]
    return row, batch


# Contacts are selectable only through the target CM's currently readable conversations.
_ACCESSIBLE = """
    SELECT 1 FROM lark_identities i
    JOIN authorizations a ON a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL
    WHERE i.identity_type='user' AND i.contact_id=c.id AND i.open_id<>a.open_id AND (
        EXISTS (SELECT 1 FROM authorization_chats ac JOIN chats ch ON ch.chat_id=ac.chat_id
            WHERE ac.authorization_id=a.id AND ac.is_visible AND ac.sync_enabled
            AND COALESCE(ch.chat_status,'normal')='normal' AND ac.p2p_counterpart_open_id=i.open_id)
        OR EXISTS (SELECT 1 FROM chat_members m
            JOIN authorization_chats ac ON ac.chat_id=m.chat_id AND ac.authorization_id=a.id
            JOIN chats ch ON ch.chat_id=ac.chat_id
            WHERE m.open_id=i.open_id AND ac.is_visible AND ac.sync_enabled
            AND COALESCE(ch.chat_status,'normal')='normal')
        OR EXISTS (SELECT 1 FROM chat_messages m JOIN authorization_message_observations o
            ON o.chat_id=m.chat_id AND o.message_id=m.message_id AND o.authorization_id=a.id
            JOIN authorization_chats ac ON ac.chat_id=o.chat_id AND ac.authorization_id=a.id
            JOIN chats ch ON ch.chat_id=ac.chat_id
            WHERE m.sender_open_id=i.open_id AND o.observed_at>=a.body_consent_at
            AND NOT m.is_deleted AND m.body_ciphertext IS NOT NULL
            AND ac.is_visible AND ac.sync_enabled AND COALESCE(ch.chat_status,'normal')='normal'))
"""


def _accessible(conn, cm_id, contact_id):
    return bool(
        conn.execute(
            "SELECT 1 FROM contacts c WHERE c.id=%s AND EXISTS (" + _ACCESSIBLE + ")", (contact_id, cm_id)
        ).fetchone()
    )


def search_users(actor_id, cm_id, q):
    q = str(q or "").strip()[:120]
    if not q:
        return []
    with connection() as conn:
        cm_id, _ = creator_handles._access(conn, actor_id, cm_id)
        rows = conn.execute(
            """WITH named AS MATERIALIZED (
                SELECT c.id,COALESCE(NULLIF(c.display_name,''),'未命名用户') display_name FROM contacts c
                WHERE POSITION(lower(%s) IN lower(COALESCE(c.display_name,'')))>0
                OR EXISTS (SELECT 1 FROM lark_identities i WHERE i.contact_id=c.id
                    AND POSITION(lower(%s) IN lower(COALESCE(i.display_name,'')))>0)
                ORDER BY display_name,c.id
            ), visible AS MATERIALIZED (
                SELECT c.* FROM named c WHERE EXISTS (""" + _ACCESSIBLE + """) LIMIT 20
            ) SELECT c.id,c.display_name,
            (SELECT COALESCE(NULLIF(ch.name,''),'相关会话') FROM authorization_chats ac
                JOIN chats ch ON ch.chat_id=ac.chat_id
                WHERE ac.authorization_id=%s AND ac.is_visible AND ac.sync_enabled
                AND COALESCE(ch.chat_status,'normal')='normal' AND EXISTS (
                    SELECT 1 FROM lark_identities i WHERE i.contact_id=c.id AND (
                        ac.p2p_counterpart_open_id=i.open_id OR EXISTS (
                            SELECT 1 FROM chat_members m WHERE m.chat_id=ac.chat_id AND m.open_id=i.open_id)))
                ORDER BY ch.chat_mode DESC,ch.chat_id LIMIT 1) context_label
            FROM visible c ORDER BY display_name,c.id""",
            (q, q, cm_id, cm_id),
        ).fetchall()
        return [
            {"id": str(r["id"]), "display_name": r["display_name"], "context_label": r["context_label"] or "有历史交流"}
            for r in rows
        ]


def _identity_conflict(conn, contact_id):
    return bool(
        conn.execute(
            """SELECT 1 FROM contact_roles WHERE contact_id=%s AND (
        status='confirmed' AND role<>'creator' AND source NOT IN ('ai_auto_confirmed','lark_internal_tenant')
        OR status='rejected' AND role='creator')
        UNION ALL SELECT 1 FROM lark_identities WHERE contact_id=%s AND identity_type='user'
        AND tenant_classification='internal' LIMIT 1""",
            (contact_id, contact_id),
        ).fetchone()
    )


def _conflict(conn, row, contact_id, handle):
    roster_type = row.get("batch_creator_type", "pending")
    if roster_type != "pending" and conn.execute(
        "SELECT 1 FROM contacts WHERE id=%s AND creator_type NOT IN ('pending',%s)",
        (contact_id, roster_type),
    ).fetchone():
        return True
    return _identity_conflict(conn, contact_id) or bool(
        conn.execute(
            """
        SELECT 1 FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL
            AND responsible_authorization_id<>%s
        UNION ALL SELECT 1 FROM creator_account_assignments WHERE contact_id=%s AND ended_at IS NULL
            AND responsible_authorization_id<>%s
        UNION ALL SELECT 1 FROM social_accounts s WHERE s.platform='tiktok'
          AND lower(s.normalized_handle)=%s AND (
            EXISTS (SELECT 1 FROM contact_social_accounts l WHERE l.social_account_id=s.id
              AND (l.status='confirmed' AND (l.contact_id<>%s OR l.relationship<>'owner')
                   OR l.contact_id=%s AND l.status='rejected'))
            OR EXISTS (SELECT 1 FROM creator_account_assignments a WHERE a.social_account_id=s.id
              AND a.ended_at IS NULL AND (a.contact_id<>%s OR a.responsible_authorization_id<>%s))) LIMIT 1
        """,
            (
                contact_id,
                row["cm_authorization_id"],
                contact_id,
                row["cm_authorization_id"],
                handle,
                contact_id,
                contact_id,
                contact_id,
                row["cm_authorization_id"],
            ),
        ).fetchone()
    )


def _admit(conn, row, actor, handle, contact_id):
    try:
        handle = normalize_handle(handle)
    except ValueError as exc:
        raise ValueError("请填写正确的 handle。") from exc
    contact_id = ownership._uuid(contact_id)
    if not handle:
        raise ValueError("请填写正确的 handle。")
    if row["review_status"] == "confirmed":
        if row["handle"] == handle and row["contact_id"] == contact_id:
            return
        raise ValueError("该条已经入库，不能覆盖已确认账号。")
    if conn.execute(
        "SELECT 1 FROM creator_import_rows WHERE batch_id=%s AND handle=%s AND id<>%s",
        (row["batch_id"], handle, row["id"]),
    ).fetchone():
        raise ValueError("这份名单中已存在相同 handle，请确认原来的那一条。")
    # Same lock ordering as the original handle flow: account, then person.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + handle,))
    if not conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (contact_id,)).fetchone():
        raise ValueError("对应用户不存在。")
    if not _accessible(conn, row["cm_authorization_id"], contact_id):
        raise PermissionError("该用户不在这位负责人的可访问联系人范围内。")
    if _conflict(conn, row, contact_id, handle):
        raise ValueError(REASONS["account_conflict"])
    accounts = conn.execute(
        "SELECT id FROM social_accounts WHERE platform='tiktok' AND lower(normalized_handle)=%s", (handle,)
    ).fetchall()
    if len(accounts) > 1:
        raise ValueError(REASONS["account_conflict"])
    account = (
        accounts[0]
        if accounts
        else conn.execute(
            """INSERT INTO social_accounts
        (id,platform,normalized_handle,display_handle) VALUES(%s,'tiktok',%s,%s) RETURNING id""",
            (uuid4(), handle, handle),
        ).fetchone()
    )
    conn.execute(
        """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,
        reviewed_by_authorization_id,reviewed_at,import_row_id)
        VALUES(%s,%s,'owner','confirmed',%s,%s,NOW(),%s)
        ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET
        import_row_id=EXCLUDED.import_row_id,updated_at=NOW()
        WHERE contact_social_accounts.status='confirmed'""",
        (
            contact_id,
            account["id"],
            "cm_confirmation",
            actor,
            row["id"],
        ),
    )
    conn.execute(
        """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,confidence,
        created_by_authorization_id,import_row_id) SELECT %s,%s,%s,%s,%s,%s,%s
        WHERE NOT EXISTS (SELECT 1 FROM contact_assignments WHERE contact_id=%s AND ended_at IS NULL)""",
        (
            uuid4(),
            contact_id,
            row["cm_authorization_id"],
            "manual",
            None,
            actor,
            row["id"],
            contact_id,
        ),
    )
    _record_roster_identity(conn, actor, contact_id)
    conn.execute(
        """UPDATE creator_import_rows SET handle=%s,contact_id=%s,review_status='confirmed',
        status='succeeded',outcome='matched',reason_code=%s,reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW()
        WHERE id=%s""",
        (handle, contact_id, "manual_confirmed", actor, row["id"]),
    )
    _audit(conn, actor, "confirmed",
           {"row_id": str(row["id"]), "cm_id": str(row["cm_authorization_id"])})


def _record_roster_identity(conn, actor, contact_id):
    """Called only after account/person locks and all admission conflict checks."""
    conn.execute("UPDATE contacts SET creator_type='cap',updated_at=NOW() WHERE id=%s", (contact_id,))
    conn.execute("UPDATE contact_roles SET is_primary=FALSE WHERE contact_id=%s AND is_primary", (contact_id,))
    conn.execute(
        """INSERT INTO contact_roles(contact_id,role,status,source,is_primary,
        reviewed_by_authorization_id,reviewed_at) VALUES(%s,'creator','confirmed','cm_confirmation',TRUE,%s,NOW())
        ON CONFLICT(contact_id,role) DO UPDATE SET status='confirmed',source=EXCLUDED.source,is_primary=TRUE,
        reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,reviewed_at=NOW(),updated_at=NOW()""",
        (contact_id, actor),
    )


def preview_import(actor_id, cm_id, filename, content, creator_type="cap"):
    if creator_type != "cap":
        raise ValueError("本次仅支持 CAP 达人名单。")
    parsed = parse_xlsx(filename, content)
    actor_id = ownership._uuid(actor_id)
    with connection() as conn, conn.transaction():
        cm_id, _ = creator_handles._access(conn, actor_id, cm_id)
        fingerprint = hashlib.sha256(content).hexdigest()
        batch = conn.execute(
            """INSERT INTO creator_import_batches
            (id,cm_authorization_id,uploaded_by,filename,file_fingerprint,policy_version,preview_rows,counts,creator_type)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(cm_authorization_id,uploaded_by,file_fingerprint,policy_version,creator_type)
            DO UPDATE SET filename=EXCLUDED.filename RETURNING *""",
            (
                uuid4(),
                cm_id,
                actor_id,
                PurePath(filename).name[:160],
                fingerprint,
                POLICY_VERSION,
                Jsonb(parsed["rows"]),
                Jsonb(parsed["counts"]),
                creator_type,
            ),
        ).fetchone()
        return {
            "id": str(batch["id"]),
            "status": batch["status"],
            "filename": batch["filename"],
            "creator_type": batch["creator_type"],
            "counts": parsed["counts"],
            "rows": parsed["rows"],
        }


def start_import(actor_id, batch_id):
    """Save a CAP roster for manual review; never look up groups or call AI."""
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] == "preview":
            if not batch["counts"].get("valid"):
                raise ValueError("没有可以导入的 handle，请修改表格。")
            for entry in batch["preview_rows"]:
                if entry["error"]:
                    continue
                row = conn.execute(
                    """INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,
                    row_number,original_handle,handle,status,reason_code,outcome)
                    VALUES(%s,%s,%s,%s,%s,%s,'skipped','manual_review','review') RETURNING *""",
                    (uuid4(), batch["id"], batch["cm_authorization_id"], entry["row_number"],
                     entry.get("original_handle", entry["handle"]), entry["handle"]),
                ).fetchone()
                row["batch_creator_type"] = "cap"
                try:
                    with conn.transaction():
                        contact_id = _existing(conn, row)
                        if contact_id:
                            if _conflict(conn, row, contact_id, row["handle"]):
                                raise ValueError(REASONS["account_conflict"])
                            person = conn.execute("SELECT creator_type FROM contacts WHERE id=%s",
                                                  (contact_id,)).fetchone()
                            # Reusing an existing CAP account only records this
                            # import's link; it never re-reviews or locks people.
                            # A previous pending CAP attribute needs manual confirmation.
                            if person["creator_type"] == "cap":
                                conn.execute(
                                    """UPDATE creator_import_rows SET contact_id=%s,review_status='confirmed',
                                    status='succeeded',outcome='matched',reason_code='existing',
                                    reviewed_at=NOW(),updated_at=NOW() WHERE id=%s""", (contact_id, row["id"]),
                                )
                                _audit(conn, ownership._uuid(actor_id), "reused", {"row_id": str(row["id"])})
                            else:
                                conn.execute("UPDATE creator_import_rows SET contact_id=%s WHERE id=%s",
                                             (contact_id, row["id"]))
                except ValueError:
                    conn.execute("UPDATE creator_import_rows SET reason_code='account_conflict',outcome='conflict' "
                                 "WHERE id=%s", (row["id"],))
            conn.execute(
                "UPDATE creator_import_batches SET status='completed',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
            _audit(conn, ownership._uuid(actor_id), "started", {"batch_id": str(batch["id"])})
            batch["status"] = "completed"
        return {"id": str(batch["id"]), "status": batch["status"]}


def confirm_row(actor_id, row_id, handle, contact_id):
    with connection() as conn, conn.transaction():
        row, batch = _row(conn, actor_id, row_id)
        if batch["status"] != "completed":
            raise ValueError("请先确认导入名单。")
        _admit(conn, row, ownership._uuid(actor_id), handle, contact_id)
        return {"id": str(row["id"]), "status": "confirmed"}


def defer_row(actor_id, row_id):
    with connection() as conn, conn.transaction():
        row, _ = _row(conn, actor_id, row_id)
        if row["review_status"] == "confirmed":
            raise ValueError("已入库的记录不能暂缓。")
        conn.execute(
            "UPDATE creator_import_rows SET review_status='deferred',updated_at=NOW() WHERE id=%s",
            (row["id"],),
        )
        _audit(conn, ownership._uuid(actor_id), "deferred", {"row_id": str(row["id"])})
        return {"id": str(row["id"]), "status": "deferred"}


def _existing(conn, row):
    found = conn.execute(
        """SELECT DISTINCT s.id,l.contact_id,(
            EXISTS (SELECT 1 FROM contact_assignments a WHERE a.contact_id=l.contact_id AND a.ended_at IS NULL
                AND a.responsible_authorization_id=%s)
            OR EXISTS (SELECT 1 FROM creator_account_assignments a WHERE a.social_account_id=s.id
                AND a.contact_id=l.contact_id AND a.responsible_authorization_id=%s AND a.ended_at IS NULL)
            ) assigned_here,(
            EXISTS (SELECT 1 FROM contact_assignments a WHERE a.contact_id=l.contact_id AND a.ended_at IS NULL
                AND a.responsible_authorization_id<>%s)
            OR EXISTS (SELECT 1 FROM creator_account_assignments a WHERE a.social_account_id=s.id
                AND a.responsible_authorization_id<>%s AND a.ended_at IS NULL)
            ) assigned_elsewhere FROM social_accounts s
        LEFT JOIN contact_social_accounts l ON l.social_account_id=s.id AND l.status='confirmed'
            AND l.relationship='owner'
        WHERE s.platform='tiktok' AND lower(s.normalized_handle)=%s""",
        (*(row["cm_authorization_id"],) * 4, row["handle"]),
    ).fetchall()
    if len(found) > 1 or any(item["assigned_elsewhere"] for item in found):
        raise ValueError(REASONS["account_conflict"])
    return found[0]["contact_id"] if found and found[0]["assigned_here"] else None


def batch_summaries(conn, cm_id):
    batches = conn.execute(
        """SELECT * FROM creator_import_batches WHERE cm_authorization_id=%s
        AND policy_version=%s ORDER BY created_at DESC,id DESC LIMIT 10""",
        (cm_id, POLICY_VERSION),
    ).fetchall()
    decorated = []
    for batch in batches:
        counts = {
            **batch["counts"],
            "confirmed": 0,
            "existing": 0,
            "review": 0,
            "queued": 0,
            "running": 0,
            "failed": 0,
            "pending": 0,
        }
        for group in conn.execute(
            """SELECT status,review_status,reason_code,count(*) n FROM creator_import_rows
            WHERE batch_id=%s GROUP BY status,review_status,reason_code""",
            (batch["id"],),
        ).fetchall():
            if group["review_status"] == "confirmed":
                counts["confirmed"] += group["n"]
                counts["existing"] += group["n"] if group["reason_code"] == "existing" else 0
            elif group["status"] in ("succeeded", "skipped", "invalid", "failed"):
                counts["review"] += group["n"]
                counts["failed"] += group["n"] if group["status"] == "failed" else 0
            else:
                counts[group["status"] if group["status"] in counts else "pending"] += group["n"]
        decorated.append(
            {
                "id": str(batch["id"]),
                "filename": batch["filename"],
                "creator_type": batch["creator_type"],
                "status": batch["status"],
                "status_label": BATCH_LABELS[batch["status"]],
                "counts": counts,
                "can_start": batch["status"] == "preview" and bool(counts["valid"]),
            }
        )
    return decorated


def _review_reason(row, visible):
    if row["contact_id"] and not visible:
        return "对应用户当前不可访问，请重新选择可访问的 Lark 用户。"
    return REASONS.get(row["reason_code"], REASONS["manual_review"])


def candidate_items(conn, cm_id, rows):
    contact_ids = list({r["contact_id"] for r in rows if r["contact_id"]})
    visible_ids = {
        r["id"] for r in conn.execute(
            "SELECT c.id FROM contacts c WHERE c.id=ANY(%s::uuid[]) AND EXISTS (" + _ACCESSIBLE + ")",
            (contact_ids, cm_id),
        ).fetchall()
    } if contact_ids else set()
    candidates = [
        {
            "id": str(r["id"]),
            "handle": r["handle"],
            "contact_id": str(r["contact_id"]) if r["contact_id"] in visible_ids else None,
            "display_name": (r["display_name"] or "—") if r["contact_id"] in visible_ids else "—",
            "creator_type": r.get("batch_creator_type", "pending"),
            "kind": "import",
            "sources": source_labels(r),
            "source": " + ".join(source_labels(r)),
            "status_label": "暂缓" if r["review_status"] == "deferred"
            else "存在冲突" if r["outcome"] == "conflict" else "待确认",
            "reason": _review_reason(r, r["contact_id"] in visible_ids),
            "confidence": None,
            "can_confirm": True,
            "can_defer": r["review_status"] != "deferred",
        }
        for r in rows
    ]
    return candidates


def page_data(actor_id, cm_id, q="", page=1):
    q, page = str(q or "").strip()[:120], max(1, min(int(page), 10000))
    with connection() as conn:
        cm_id, _ = creator_handles._access(conn, actor_id, cm_id)
        decorated = batch_summaries(conn, cm_id)
        base = """FROM creator_import_rows r JOIN creator_import_batches b ON b.id=r.batch_id
            WHERE r.cm_authorization_id=%s AND b.policy_version=%s
            AND b.status<>'preview' AND r.review_status<>'confirmed'"""
        params = (cm_id, POLICY_VERSION)
        if q:
            base += """ AND (POSITION(lower(%s) IN lower(r.handle))>0 OR EXISTS (
                SELECT 1 FROM contacts c WHERE c.id=r.contact_id
                AND POSITION(lower(%s) IN lower(COALESCE(c.display_name,'')))>0
                AND EXISTS (""" + _ACCESSIBLE + ")))"
            params += (q, q, cm_id)
        total = conn.execute("SELECT count(*) n " + base, params).fetchone()["n"]
        page = min(page, max(1, math.ceil(total / 25)))
        # Paginate before checking identity access; a refresh must never enumerate all CM-visible users.
        rows = conn.execute(
            "WITH page_rows AS MATERIALIZED (SELECT r.*,b.creator_type batch_creator_type "
            + base
            + " ORDER BY r.created_at DESC,r.id LIMIT 25 OFFSET %s) "
            "SELECT r.*,c.display_name,c.creator_type FROM page_rows r "
            "LEFT JOIN contacts c ON c.id=r.contact_id "
            "ORDER BY r.created_at DESC,r.id",
            (*params, (page - 1) * 25),
        ).fetchall()
        candidates = candidate_items(conn, cm_id, rows)
        return {
            "batches": decorated,
            "candidates": candidates,
            "total": total,
            "page": page,
            "total_pages": max(1, math.ceil(total / 25)),
        }

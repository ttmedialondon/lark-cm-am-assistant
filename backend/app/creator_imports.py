"""Single-CM CAP rosters: scoped evidence, AI group/person decisions and admission."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import PurePath
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import ai_dispatch, contact_assessments, creator_handles, creator_import_evidence
from . import creator_ownership as ownership
from .ai_feature_config import api_key_cipher
from .config import get_settings
from .creator_handle_evidence import normalize_handle
from .creator_import_xlsx import MAX_FILE_BYTES, parse_xlsx  # noqa: F401 - routes share the upload bound
from .db import connection

POLICY_VERSION = "creator-import-v2"
LOGGER = logging.getLogger(__name__)
REASONS = {
    "existing": "已有相同账号和负责人，已复用正式关联",
    "group_review": "群对应关系尚未确定，请查看证据链。",
    "person_review": "群已定位，用户归属尚未确定，请查看证据链。",
    "group_not_found": "未找到可靠的对应群，请查看搜索线索。",
    "person_not_found": "尚未定位对应的 Lark 用户。",
    "configuration_changed": "AI 配置不可用或已变化，请检查配置后重新分析。",
    "authorization_changed": "相关授权或材料已变化，请重新检查。",
    "preparation_failed": "证据准备异常，可以重新检查。",
    "interrupted": "分析中断，未自动重复付费请求。",
    "provider_error": "AI 请求异常，可以重新检查。",
    "invalid_result": "AI 结果未通过证据校验，请人工核对。",
    "rate_limited": "AI 暂时限速，等待重试。",
    "other_batch": "相同账号已在其他批次分析，请稍后重新检查。",
    "material_limit": "候选材料超出本次上限，请查看证据链并核对。",
    "manual_review": "请核对 handle，选择对应的 Lark 用户后确认入库。",
    "manual_confirmed": "已确认入库",
    "ai_confirmed": "AI 已确认群和用户，证据及冲突检查通过，已自动入库。",
    "group_uncertain": "群对应关系尚未确定，请查看证据链。",
    "person_uncertain": "群已定位，但这个 handle 对应的用户尚未确定。",
    "group_unavailable": "对应群当前不可访问，请检查授权。",
    "no_readable_material": "负责人当前没有有效的可读聊天授权。",
    "account_conflict": "存在账号、人员分类、CAP 属性或负责人冲突，未覆盖原资料",
}
BATCH_LABELS = {"preview": "待确认导入", "running": "正在识别", "paused": "已暂停", "completed": "识别完成"}
PHASE_LABELS = {"group": "AI 找群", "person": "AI 找人", "completed": "已完成"}


def source_labels(row):
    return (
        ["Excel", "AI"]
        if row.get("api_requests", 0) and not (row.get("stats") or {}).get("ai_invalidated")
        else ["Excel"]
    )


def _audit(conn, actor, action, data):
    conn.execute(
        "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s)",
        (uuid4(), actor, "creator_import_" + action, Jsonb(json.loads(ownership._encode(data)))),
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
                SELECT c.* FROM named c WHERE EXISTS ("""
            + _ACCESSIBLE
            + """) LIMIT 20
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
    if (
        roster_type != "pending"
        and conn.execute(
            "SELECT 1 FROM contacts WHERE id=%s AND creator_type NOT IN ('pending',%s)",
            (contact_id, roster_type),
        ).fetchone()
    ):
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


def _admit(conn, row, actor, handle, contact_id, *, automatic=False):
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
            "excel_ai_verified" if automatic else "cm_confirmation",
            None if automatic else actor,
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
            "ai" if automatic else "manual",
            row.get("confidence") if automatic else None,
            None if automatic else actor,
            row["id"],
            contact_id,
        ),
    )
    if not automatic and (row["handle"] != handle or row["contact_id"] != contact_id):
        _invalidate_chain(conn, row, actor)
    _record_roster_identity(conn, actor, contact_id, automatic=automatic)
    conn.execute(
        """UPDATE creator_import_rows SET handle=%s,contact_id=%s,review_status='confirmed',
        status='succeeded',outcome='matched',reason_code=%s,reviewed_by=%s,reviewed_at=NOW(),lease_until=NULL,
        stats=stats||'{"analysis_stage":"completed"}'::jsonb,updated_at=NOW()
        WHERE id=%s""",
        (
            handle,
            contact_id,
            "ai_confirmed" if automatic else "manual_confirmed",
            None if automatic else actor,
            row["id"],
        ),
    )
    _audit(
        conn,
        actor,
        "auto_admitted" if automatic else "confirmed",
        {"row_id": str(row["id"]), "cm_id": str(row["cm_authorization_id"])},
    )


def _record_roster_identity(conn, actor, contact_id, *, automatic=False):
    """Called only after account/person locks and all admission conflict checks."""
    conn.execute(
        "UPDATE contacts SET creator_type='cap',updated_at=NOW() WHERE id=%s AND creator_type<>'cap'", (contact_id,)
    )
    if (
        automatic
        and conn.execute(
            "SELECT 1 FROM contact_roles WHERE contact_id=%s AND role='creator' AND status='confirmed' AND is_primary",
            (contact_id,),
        ).fetchone()
    ):
        return
    conn.execute("UPDATE contact_roles SET is_primary=FALSE WHERE contact_id=%s AND is_primary", (contact_id,))
    conn.execute(
        """INSERT INTO contact_roles(contact_id,role,status,source,is_primary,
        reviewed_by_authorization_id,reviewed_at) VALUES(%s,'creator','confirmed',%s,TRUE,%s,NOW())
        ON CONFLICT(contact_id,role) DO UPDATE SET status='confirmed',source=EXCLUDED.source,is_primary=TRUE,
        reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,reviewed_at=NOW(),updated_at=NOW()""",
        (contact_id, "excel_ai_verified" if automatic else "cm_confirmation", None if automatic else actor),
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
    """Record the roster immediately; prepare evidence and run AI in background."""
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
                    row_number,original_handle,handle,status,reason_code,outcome,stats)
                    VALUES(%s,%s,%s,%s,%s,%s,'pending','','review','{"analysis_stage":"group"}') RETURNING *""",
                    (
                        uuid4(),
                        batch["id"],
                        batch["cm_authorization_id"],
                        entry["row_number"],
                        entry.get("original_handle", entry["handle"]),
                        entry["handle"],
                    ),
                ).fetchone()
                row["batch_creator_type"] = "cap"
                try:
                    with conn.transaction():
                        contact_id = _existing(conn, row)
                        if contact_id:
                            if _conflict(conn, row, contact_id, row["handle"]):
                                raise ValueError(REASONS["account_conflict"])
                            person = conn.execute(
                                "SELECT creator_type FROM contacts WHERE id=%s", (contact_id,)
                            ).fetchone()
                            # Reusing an existing CAP account only records this
                            # import's link; it never re-reviews or locks people.
                            # A previous pending CAP attribute needs manual confirmation.
                            if person["creator_type"] == "cap":
                                conn.execute(
                                    """UPDATE creator_import_rows SET contact_id=%s,review_status='confirmed',
                                    status='succeeded',outcome='matched',reason_code='existing',
                                    reviewed_at=NOW(),updated_at=NOW() WHERE id=%s""",
                                    (contact_id, row["id"]),
                                )
                                _audit(conn, ownership._uuid(actor_id), "reused", {"row_id": str(row["id"])})
                            else:
                                conn.execute(
                                    "UPDATE creator_import_rows SET contact_id=%s WHERE id=%s", (contact_id, row["id"])
                                )
                except ValueError:
                    conn.execute(
                        "UPDATE creator_import_rows SET status='skipped',reason_code='account_conflict',"
                        "outcome='conflict' "
                        "WHERE id=%s",
                        (row["id"],),
                    )
            conn.execute(
                "UPDATE creator_import_batches SET status='running',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
            _settle(conn, batch["id"])
            _audit(conn, ownership._uuid(actor_id), "started", {"batch_id": str(batch["id"])})
            batch = conn.execute("SELECT * FROM creator_import_batches WHERE id=%s", (batch["id"],)).fetchone()
        return {"id": str(batch["id"]), "status": batch["status"]}


def confirm_row(actor_id, row_id, handle, contact_id):
    with connection() as conn, conn.transaction():
        row, batch = _row(conn, actor_id, row_id)
        if batch["status"] == "preview":
            raise ValueError("请先确认导入名单。")
        _admit(conn, row, ownership._uuid(actor_id), handle, contact_id)
        _settle(conn, row["batch_id"])
        return {"id": str(row["id"]), "status": "confirmed"}


def defer_row(actor_id, row_id):
    with connection() as conn, conn.transaction():
        row, _ = _row(conn, actor_id, row_id)
        if row["review_status"] == "confirmed":
            raise ValueError("已入库的记录不能暂缓。")
        conn.execute(
            "UPDATE creator_import_rows SET review_status='deferred',status='skipped',"
            "lease_until=NULL,updated_at=NOW() WHERE id=%s",
            (row["id"],),
        )
        _audit(conn, ownership._uuid(actor_id), "deferred", {"row_id": str(row["id"])})
        _settle(conn, row["batch_id"])
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
            "preparing": 0,
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
                "can_pause": batch["status"] == "running",
                "can_resume": batch["status"] == "paused",
                "can_recheck": batch["status"] in {"paused", "completed"} and counts["review"] > 0,
            }
        )
    return decorated


def _review_reason(row, visible):
    if row["contact_id"] and not visible:
        return "对应用户当前不可访问，请重新选择可访问的 Lark 用户。"
    if row["status"] in {"pending", "preparing", "queued", "running"}:
        return "系统正在找群、找人；证据充分且无冲突时会自动入库。"
    return REASONS.get(row["reason_code"], REASONS["manual_review"])


def _status_label(row):
    if row["review_status"] == "confirmed":
        return "已入库"
    if row["review_status"] == "deferred":
        return "暂缓"
    if row["outcome"] == "conflict":
        return "存在冲突"
    return {
        "pending": "等待处理",
        "preparing": "正在收集证据",
        "queued": "等待 AI",
        "running": "AI 分析中",
        "failed": "分析异常",
        "invalid": "待核对",
    }.get(row["status"], "待核对")


def candidate_items(conn, cm_id, rows):
    settings = get_settings()
    chains = {r["id"]: _decode(r, settings) for r in rows}
    group_ids = list(
        {
            ref["chat_id"]
            for chain in chains.values()
            for material in chain.values()
            for ref in material.get("refs", [])
            if ref.get("kind") == "group"
        }
    )
    names = (
        {
            g["chat_id"]: g["name"]
            for g in conn.execute(
                """SELECT c.chat_id,c.name FROM authorization_chats ac JOIN chats c ON c.chat_id=ac.chat_id
           JOIN authorizations a ON a.id=ac.authorization_id
           WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL
             AND ac.chat_id=ANY(%s::text[]) AND ac.is_visible AND ac.sync_enabled
             AND COALESCE(c.chat_status,'normal')='normal'""",
                (cm_id, group_ids),
            ).fetchall()
        }
        if group_ids
        else {}
    )
    contact_ids = list({r["contact_id"] for r in rows if r["contact_id"]})
    visible_ids = (
        {
            r["id"]
            for r in conn.execute(
                "SELECT c.id FROM contacts c WHERE c.id=ANY(%s::uuid[]) AND EXISTS (" + _ACCESSIBLE + ")",
                (contact_ids, cm_id),
            ).fetchall()
        }
        if contact_ids
        else set()
    )
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
            "status_label": _status_label(r),
            "phase_label": PHASE_LABELS.get(_stage(r), "等待处理"),
            "group_names": list(
                dict.fromkeys(
                    names[ref["chat_id"]]
                    for material in chains[r["id"]].values()
                    for ref in material.get("refs", [])
                    if ref.get("kind") == "group" and ref["chat_id"] in names
                )
            ),
            "has_evidence": bool(r.get("pack_ciphertext")),
            "reason": _review_reason(r, r["contact_id"] in visible_ids),
            "confidence": float(r["confidence"]) if r.get("confidence") is not None else None,
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
            base += (
                """ AND (POSITION(lower(%s) IN lower(r.handle))>0 OR EXISTS (
                SELECT 1 FROM contacts c WHERE c.id=r.contact_id
                AND POSITION(lower(%s) IN lower(COALESCE(c.display_name,'')))>0
                AND EXISTS ("""
                + _ACCESSIBLE
                + ")))"
            )
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


def _decode(row, settings, key="pack_ciphertext"):
    return json.loads(ownership._cipher(settings).decrypt(row[key])) if row.get(key) else {}


def row_evidence(actor_id, row_id):
    settings = get_settings()
    with connection() as conn:
        row, batch = _row(conn, actor_id, row_id)
        result = {
            "handle": row["handle"],
            "source": " + ".join(source_labels(row)),
            "status_label": _status_label(row),
            "phase_label": PHASE_LABELS.get(_stage(row), ""),
            "reason": _review_reason(row, True),
            "groups": [],
            "person": {},
            "conflicts": [],
            "warnings": [],
        }
        chain, decisions = _decode(row, settings), _decode(row, settings, "result_ciphertext")
        if not chain:
            result["warnings"].append("尚未收集证据，或修改后原判断已失效。")
            return result
        if not _valid_chain(conn, settings, row, chain, decisions):
            result["warnings"].append("原始材料或访问权限已变化，旧证据暂不展示，请重新检查。")
            return result
        groups = {}
        cm = conn.execute(
            "SELECT display_name FROM authorizations WHERE id=%s", (batch["cm_authorization_id"],)
        ).fetchone()
        selected = _selected_chats(chain, decisions) if decisions.get("group", {}).get("outcome") == "matched" else []
        classifications = {
            "creator": "达人",
            "unclassified": "未分类",
            "internal_colleague": "同事",
            "merchant": "商家",
            "merchant_contact": "商家联系人",
            "partner_contact": "合作方联系人",
            "platform_contact": "平台联系人",
            "other_external_contact": "其他外部联系人",
            "agency": "机构",
            "other": "其他",
        }
        exclusions = {
            "bot": "机器人，已排除",
            "internal_colleague": "内部同事，已排除",
            "confirmed_noncreator": "已有非达人分类，已排除",
            "missing_contact": "缺少可用 Lark 用户",
        }
        for stage in ("group", "person"):
            material = chain.get(stage)
            if not material:
                continue
            pack, refs = material["pack"], {r["label"]: r for r in material["refs"]}
            decision = decisions.get(stage, {})
            phase = "找群" if stage == "group" else "找人"
            ai_outcome = decision.get("outcome")
            # Historical decisions retain the rules under which they were admitted.
            if (pack.get("policy_version") == creator_import_evidence.POLICY_VERSION
                    and (decision.get("response") or {}).get("text")):
                validator = (
                    creator_import_evidence.group_validate
                    if stage == "group"
                    else (creator_import_evidence.person_validate)
                )
                try:
                    raw_decision = json.loads(decision["response"]["text"])
                    ai_outcome = raw_decision.get("outcome") if isinstance(raw_decision, dict) else None
                    decision = {**decision, **validator(decision["response"]["text"], pack)}
                except (ValueError, TypeError, KeyError):
                    result["warnings"].append(phase + "：AI 输出格式或引用未通过校验。")
            result["warnings"].extend(phase + "：" + note for note in decision.get("blocking_reasons", []))
            decision_label = {"matched": "对应关系通过", "review": "对应关系待定", "not_found": "未找到对应对象"}.get(
                decision.get("outcome"), "等待判断"
            )
            if ai_outcome == "matched" and decision.get("outcome") != "matched":
                decision_label = "AI 建议匹配，证据校验未通过"
            if decision.get("invalidated"):
                decision_label = "AI 结果已失效"

            def prefix(label, phase=phase):
                return phase + " " + label

            def explain(value, phase=phase):
                return re.sub(
                    r"(?<![A-Za-z0-9_])([GUMX]\d+)(?![A-Za-z0-9_])", lambda match: phase + " " + match[0], value or ""
                )

            if stage == "group":
                result["group_decision"] = {
                    "status_label": decision_label,
                    "confidence": decision.get("confidence"),
                    "reason": explain(decision.get("reason")),
                    "evidence_refs": [prefix(ref) for ref in decision.get("evidence_refs", [])],
                }
                if "history_search_messages" in material["stats"]:
                    result["group_decision"]["search_summary"] = (
                        f"历史线索补查：{material['stats']['history_search_messages']:,} 条可读群消息，"
                        f"命中 {material['stats']['history_matching_groups']} 个群，"
                        f"本次提供 {len(pack['groups'])} 个群。"
                    )
            users = {u["ref"]: u for u in pack["users"]}
            names = {
                ref: item.get("display_name") or ref
                for ref, item in refs.items()
                if item["kind"] in {"person", "unresolved_person"}
            }
            names["CM1"] = cm["display_name"] or "负责人"
            assessments = {a["user_ref"]: a for a in decision.get("candidates", [])}
            for group in pack["groups"]:
                ref = refs[group["ref"]]
                item = groups.setdefault(
                    ref["chat_id"],
                    {
                        "ref": prefix(group["ref"]),
                        "name": ref["display_name"],
                        "selected": ref["chat_id"] in selected,
                        "confidence": None,
                        "reason": "",
                        "members": [],
                        "messages": [],
                    },
                )
                if stage == "group":
                    item["confidence"] = decision.get("confidence") if item["selected"] else None
                    item["reason"] = explain(decision.get("reason")) or (
                        "群名包含完整 handle，等待 AI 判断。"
                        if group["match_kind"] == "exact"
                        else "历史发言提及此 handle，需要 AI 核对是否为对应群。"
                        if group["match_kind"] == "history"
                        else "群名有相似拼写，需结合线索判断。"
                    )
                if stage == "person":
                    item["ref"] += " / " + prefix(group["ref"])
                    item["members"] = [
                        {
                            "ref": "CM1",
                            "display_name": names["CM1"],
                            "classification": "本批负责人",
                            "status_label": "已排除",
                            "reason": "由此人负责本批 CAP 名单。",
                        }
                    ]
                    for label in group["member_refs"]:
                        user = users[label]
                        assessment = assessments.get(label, {})
                        item["members"].append(
                            {
                                "ref": prefix(label),
                                "display_name": names.get(label, "—"),
                                "classification": classifications.get(user["known_category"], user["known_category"]),
                                "status_label": {
                                    "target": "对应此账号",
                                    "not_target": "不是此账号",
                                    "uncertain": "待定",
                                }.get(assessment.get("assessment"), "待判断" if user["eligible"] else "已排除"),
                                "reason": exclusions.get(user["exclusion_reason"])
                                or explain(assessment.get("reason"))
                                or (
                                    "群成员；无需先有本人发言。"
                                    if "member" in user["membership_sources"]
                                    else "仅在历史发言中出现，不能据此认定仍在群内。"
                                ),
                            }
                        )
                for message in pack["messages"]:
                    if message["group_ref"] == group["ref"]:
                        item["messages"].append(
                            {
                                "ref": prefix(message["ref"]),
                                "sender_name": names.get(message["speaker"], message["speaker"]),
                                "time_label": message.get("sent_at") or "",
                                "text": ("【节选】" if message.get("excerpted") else "") + message["text"],
                                "available": True,
                            }
                        )
            if stage == "person":
                result["person"] = {
                    "status_label": decision_label,
                    "display_name": names.get(decision.get("user_ref"), "—"),
                    "confidence": decision.get("confidence"),
                    "reason": explain(decision.get("reason")) or "等待 AI 定位用户。",
                    "evidence_refs": [prefix(ref) for ref in decision.get("evidence_refs", [])],
                }
                result["conflicts"].extend(explain(conflict) for conflict in decision.get("conflicts", []))
            for key, warning in (
                ("group_candidates_truncated", "群搜索或候选范围未完整覆盖，本次线索需人工核对。"),
                ("history_search_truncated", "群历史检索达到本次上限，更早的消息尚未完整检查。"),
                ("users_truncated", "可选用户超过取材上限。"),
                ("history_truncated", "历史消息超过取材上限，仅检查最近的部分记录。"),
                ("messages_omitted", "AI 仅接收关键发言；未发送完整聊天记录。"),
                ("critical_evidence_omitted", "部分账号或改名线索未能放入本次材料，需核对。"),
            ):
                if material["stats"].get(key):
                    result["warnings"].append(phase + "：" + warning)
            if decision.get("invalidated"):
                result["warnings"].append(phase + "结果已失效，未用于入库。")
        result["groups"] = list(groups.values())
        if row["outcome"] == "conflict":
            result["conflicts"].append(REASONS["account_conflict"])
        return result


def _stage(row):
    return (row.get("stats") or {}).get("analysis_stage", "group")


def _invalidate_chain(conn, row, actor):
    if row.get("pack_ciphertext") or row.get("result_ciphertext"):
        _audit(
            conn,
            actor,
            "evidence_invalidated",
            {
                "row_id": str(row["id"]),
                "previous": {
                    key: row.get(key)
                    for key in ("handle", "contact_id", "pack_ciphertext", "result_ciphertext", "refs", "stats")
                },
            },
        )
    stats = {**row.get("stats", {}), "ai_invalidated": True, "analysis_stage": "completed"}
    stats["analysis_generation"] = stats.get("analysis_generation", 0) + 1
    conn.execute(
        """UPDATE creator_import_rows SET pack_ciphertext=NULL,result_ciphertext=NULL,refs='[]',
        fingerprint=NULL,config_fingerprint=NULL,confidence=NULL,lease_until=NULL,stats=%s WHERE id=%s""",
        (Jsonb(stats), row["id"]),
    )


def _settle(conn, batch_id):
    conn.execute(
        """UPDATE creator_import_batches b SET status='completed',updated_at=NOW()
        WHERE id=%s AND status='running' AND NOT EXISTS (SELECT 1 FROM creator_import_rows r
        WHERE r.batch_id=b.id AND r.review_status='pending'
        AND r.status IN ('pending','preparing','queued','running'))""",
        (batch_id,),
    )


def pause_import(actor_id, batch_id):
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] == "running":
            conn.execute(
                "UPDATE creator_import_batches SET status='paused',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
        return {"id": str(batch["id"])}


def resume_import(actor_id, batch_id):
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] == "paused":
            conn.execute(
                "UPDATE creator_import_batches SET status='running',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
            _settle(conn, batch["id"])
        return {"id": str(batch["id"])}


def recheck_batch(actor_id, batch_id, row_ids=None):
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] not in ("completed", "paused"):
            raise ValueError("请先等待本批结束或暂停，再重新检查。")
        selected_ids = [ownership._uuid(row_id) for row_id in row_ids] if row_ids is not None else None
        rows = conn.execute(
            """SELECT * FROM creator_import_rows WHERE batch_id=%s AND review_status='pending'
               AND (%s::uuid[] IS NULL OR id=ANY(%s::uuid[])) FOR UPDATE""",
            (batch["id"], selected_ids, selected_ids),
        ).fetchall()
        for row in rows:
            _invalidate_chain(conn, row, ownership._uuid(actor_id))
            conn.execute(
                """UPDATE creator_import_rows SET status='pending',contact_id=NULL,outcome=NULL,
                reason_code='',attempts=0,scheduled_at=NOW(),updated_at=NOW(),
                stats=stats||'{"analysis_stage":"group","ai_invalidated":false}'::jsonb WHERE id=%s""",
                (row["id"],),
            )
        if rows:
            conn.execute(
                "UPDATE creator_import_batches SET status='running',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
        return {"id": str(batch["id"]), "queued": len(rows)}


def _selected_chats(chain, results):
    decision = results["group"]
    # Previously admitted evidence used a single group_ref; keep it readable.
    refs = decision.get("group_refs", [decision["group_ref"]] if decision.get("group_ref") else [])
    return creator_import_evidence.resolve_groups(chain["group"]["refs"], refs)


def _build_material(conn, settings, row, stage, chain, results):
    if stage == "group":
        return creator_import_evidence.build_group_evidence(conn, settings, row["cm_authorization_id"], row["handle"])
    return creator_import_evidence.build_person_evidence(
        conn, settings, row["cm_authorization_id"], row["handle"], _selected_chats(chain, results)
    )


def _valid_chain(conn, settings, row, chain, results, *, full=False):
    for stage, material in chain.items():
        if not creator_import_evidence.validate_references(conn, settings, row["cm_authorization_id"], material):
            return False
        if (
            full
            and _build_material(conn, settings, row, stage, chain, results)["fingerprint"] != material["fingerprint"]
        ):
            return False
    return True


def prepare_next_import(settings):
    with connection() as conn, conn.transaction():
        conn.execute(
            """UPDATE creator_import_rows r SET status='pending',lease_until=NULL
            FROM creator_import_batches b WHERE b.id=r.batch_id AND b.policy_version=%s
            AND r.status='preparing' AND r.lease_until<NOW() AND r.review_status='pending'""",
            (POLICY_VERSION,),
        )
        batch = conn.execute(
            """SELECT b.id FROM creator_import_batches b WHERE b.status='running'
            AND b.policy_version=%s AND EXISTS (SELECT 1 FROM creator_import_rows r WHERE r.batch_id=b.id
            AND r.status='pending' AND r.review_status='pending') ORDER BY b.created_at,b.id
            FOR UPDATE OF b SKIP LOCKED LIMIT 1""",
            (POLICY_VERSION,),
        ).fetchone()
        if not batch:
            return False
        row = conn.execute(
            """SELECT r.*,b.uploaded_by FROM creator_import_rows r JOIN creator_import_batches b
            ON b.id=r.batch_id WHERE r.batch_id=%s AND r.status='pending' AND r.review_status='pending'
            ORDER BY r.row_number,r.id FOR UPDATE OF r SKIP LOCKED LIMIT 1""",
            (batch["id"],),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE creator_import_rows SET status='preparing',lease_until=NOW()+INTERVAL '10 minutes' WHERE id=%s",
            (row["id"],),
        )
    stage, material, config, reason = _stage(row), None, None, ""
    try:
        with connection() as conn:
            creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
            chain, results = _decode(row, settings), _decode(row, settings, "result_ciphertext")
            if chain and not _valid_chain(conn, settings, row, chain, results, full=True):
                raise ValueError("authorization_changed")
            material = _build_material(conn, settings, row, stage, chain, results)
            config = ownership._config(conn)
            reason = material["reason_code"] if not material["eligible"] else "" if config else "configuration_changed"
    except (ValueError, PermissionError):
        reason = "authorization_changed"
    except Exception:
        LOGGER.error("creator import evidence preparation failed row=%s", row["id"])
        reason = "preparation_failed"
    with connection() as conn, conn.transaction():
        batch = conn.execute(
            "SELECT * FROM creator_import_batches WHERE id=%s FOR UPDATE", (row["batch_id"],)
        ).fetchone()
        current = conn.execute("SELECT * FROM creator_import_rows WHERE id=%s FOR UPDATE", (row["id"],)).fetchone()
        if not current or batch["policy_version"] != POLICY_VERSION:
            return True
        try:
            creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
        except PermissionError:
            reason = "authorization_changed"
        if (
            current["status"] != "preparing"
            or current["review_status"] != "pending"
            or (current["stats"].get("analysis_generation", 0) != row["stats"].get("analysis_generation", 0))
        ):
            return True
        chain = _decode(current, settings)
        if material:
            chain[stage] = material
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,137035))",
            (str(row["cm_authorization_id"]) + ":" + row["handle"],),
        )
        if (
            not reason
            and conn.execute(
                """SELECT 1 FROM creator_import_rows WHERE cm_authorization_id=%s
            AND handle=%s AND id<>%s AND status IN ('queued','running') LIMIT 1""",
                (row["cm_authorization_id"], row["handle"], row["id"]),
            ).fetchone()
        ):
            reason = "other_batch"
        stats = {**current["stats"], **(material or {}).get("stats", {}), "analysis_stage": stage}
        conn.execute(
            """UPDATE creator_import_rows SET status=%s,reason_code=%s,pack_ciphertext=%s,refs=%s,
            fingerprint=%s,config_fingerprint=%s,stats=%s,lease_until=NULL,updated_at=NOW() WHERE id=%s""",
            (
                "invalid" if reason else "queued",
                reason,
                ownership._cipher(settings).encrypt(ownership._encode(chain)),
                Jsonb((material or {}).get("refs", [])),
                (material or {}).get("fingerprint"),
                ownership._config_fingerprint(config) if config else None,
                Jsonb(stats),
                row["id"],
            ),
        )
        _settle(conn, row["batch_id"])
    return True


def claim_next_import():
    with connection() as conn, conn.transaction():
        expired = conn.execute(
            "SELECT DISTINCT batch_id FROM creator_import_rows "
            "WHERE status='running' AND lease_until<NOW() ORDER BY batch_id"
        ).fetchall()
        for batch in expired:
            if not conn.execute(
                "SELECT id FROM creator_import_batches WHERE id=%s FOR UPDATE SKIP LOCKED", (batch["batch_id"],)
            ).fetchone():
                continue
            conn.execute(
                """UPDATE creator_import_rows SET status='failed',reason_code='interrupted',lease_until=NULL
                WHERE batch_id=%s AND status='running' AND lease_until<NOW()""",
                (batch["batch_id"],),
            )
            _settle(conn, batch["batch_id"])
        if not ai_dispatch.can_claim(conn):
            return None
        row = conn.execute(
            """SELECT r.*,b.uploaded_by FROM creator_import_rows r JOIN creator_import_batches b
            ON b.id=r.batch_id WHERE b.status='running' AND b.policy_version=%s AND r.status='queued'
            AND r.review_status='pending' AND r.attempts<2 AND r.scheduled_at<=NOW()
            ORDER BY r.scheduled_at,r.created_at,r.id FOR UPDATE OF r SKIP LOCKED LIMIT 1""",
            (POLICY_VERSION,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """UPDATE creator_import_rows SET status='running',attempts=attempts+1,
            lease_until=NOW()+INTERVAL '10 minutes',updated_at=NOW() WHERE id=%s""",
            (row["id"],),
        )
        return {**row, "status": "running", "attempts": row["attempts"] + 1}


def _current(conn, job):
    conn.execute("SELECT id FROM creator_import_batches WHERE id=%s FOR UPDATE", (job["batch_id"],))
    row = conn.execute(
        """SELECT r.*,b.status batch_status,b.uploaded_by,b.creator_type batch_creator_type
        FROM creator_import_rows r JOIN creator_import_batches b ON b.id=r.batch_id
        WHERE r.id=%s AND b.policy_version=%s FOR UPDATE OF r""",
        (job["id"], POLICY_VERSION),
    ).fetchone()
    return (
        row
        if row
        and row["status"] == "running"
        and row["review_status"] == "pending"
        and (
            row["attempts"] == job["attempts"]
            and row["fingerprint"] == job["fingerprint"]
            and _stage(row) == _stage(job)
            and row["stats"].get("analysis_generation", 0) == job["stats"].get("analysis_generation", 0)
        )
        else None
    )


def _record_response(conn, job, settings, response, *, late=False):
    if not response:
        return
    row = conn.execute("SELECT stats FROM creator_import_rows WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
    key = f"{job['stats'].get('analysis_generation', 0)}:{_stage(job)}:{job['attempts']}"
    recorded = row["stats"].get("recorded_responses", [])
    if key in recorded:
        return
    usage = response.get("usage", {})
    conn.execute(
        """UPDATE creator_import_rows SET input_tokens=input_tokens+%s,output_tokens=output_tokens+%s,
        response_id=%s,request_id=%s,stats=stats||%s WHERE id=%s""",
        (
            usage.get("input_tokens") or 0,
            usage.get("output_tokens") or 0,
            response.get("response_id"),
            response.get("telemetry", {}).get("openai_request_id"),
            Jsonb({"recorded_responses": [*recorded, key]}),
            job["id"],
        ),
    )
    if late:
        _audit(
            conn,
            job["uploaded_by"],
            "late_response",
            {
                "row_id": str(job["id"]),
                "stage": _stage(job),
                "response_ciphertext": ownership._cipher(settings).encrypt(ownership._encode(response)),
            },
        )


def _finish(job, settings, *, response=None, result=None, reason=""):
    with connection() as conn, conn.transaction():
        row = _current(conn, job)
        _record_response(conn, job, settings, response, late=not row)
        if not row:
            return
        chain, results = _decode(row, settings), _decode(row, settings, "result_ciphertext")
        if result:
            try:
                creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
                config = ownership._config(conn)
                if not config or ownership._config_fingerprint(config) != row["config_fingerprint"]:
                    raise ValueError("configuration_changed")
                if not _valid_chain(conn, settings, row, chain, results, full=True):
                    raise ValueError("authorization_changed")
            except (ValueError, PermissionError) as exc:
                reason = str(exc) if str(exc) in REASONS else "authorization_changed"
        stage = _stage(row)
        results[stage] = {**(result or {}), "response": response}
        if reason:
            results[stage]["invalidated"] = True
        matched = bool(result and not reason and result["outcome"] == "matched" and result["confidence"] >= 0.95)
        contact_id = None
        if matched and stage == "person":
            contact_id = creator_import_evidence.resolve_contact(chain["person"]["refs"], result.get("user_ref"))
            matched = bool(contact_id and not result.get("conflicts"))
        next_stage = "person" if matched and stage == "group" else "completed"
        status = "pending" if next_stage == "person" else "invalid" if reason else "succeeded"
        reason = reason or ("" if matched else (result or {}).get("reason_code") or f"{stage}_review")
        conn.execute(
            """UPDATE creator_import_rows SET status=%s,reason_code=%s,contact_id=%s,outcome=%s,
            confidence=%s,result_ciphertext=%s,attempts=CASE WHEN %s THEN 0 ELSE attempts END,
            stats=stats||%s,lease_until=NULL,updated_at=NOW() WHERE id=%s""",
            (
                status,
                reason,
                contact_id,
                "matched" if matched else "review",
                (result or {}).get("confidence"),
                ownership._cipher(settings).encrypt(ownership._encode(results)),
                next_stage == "person",
                Jsonb(
                    {
                        "analysis_stage": next_stage,
                        "ai_invalidated": bool(reason in ("authorization_changed", "configuration_changed")),
                    }
                ),
                row["id"],
            ),
        )
        if matched and stage == "person":
            try:
                with conn.transaction():
                    saved = conn.execute("SELECT * FROM creator_import_rows WHERE id=%s", (row["id"],)).fetchone()
                    _admit(
                        conn,
                        {**saved, "batch_creator_type": "cap"},
                        row["uploaded_by"],
                        row["handle"],
                        contact_id,
                        automatic=True,
                    )
            except (ValueError, PermissionError):
                conn.execute(
                    "UPDATE creator_import_rows SET outcome='conflict',reason_code='account_conflict' WHERE id=%s",
                    (row["id"],),
                )
        _settle(conn, row["batch_id"])


def run_claimed_import(job, settings):
    response = None
    try:
        with connection() as conn, conn.transaction():
            row = _current(conn, job)
            if not row:
                return
            if row["batch_status"] == "paused":
                conn.execute(
                    "UPDATE creator_import_rows SET status='queued',attempts=attempts-1,lease_until=NULL WHERE id=%s",
                    (row["id"],),
                )
                return
            creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
            chain, results = _decode(row, settings), _decode(row, settings, "result_ciphertext")
            if not _valid_chain(conn, settings, row, chain, results, full=True):
                raise ValueError("authorization_changed")
            config = ownership._config(conn)
            if not config or ownership._config_fingerprint(config) != row["config_fingerprint"]:
                raise ValueError("configuration_changed")
            key = api_key_cipher(settings.token_encryption_key).decrypt(config["api_key_ciphertext"])
            pack = chain[_stage(row)]["pack"]
            conn.execute("UPDATE creator_import_rows SET api_requests=api_requests+1 WHERE id=%s", (row["id"],))
        group = _stage(job) == "group"
        response = contact_assessments.call_openai(
            config,
            key,
            pack,
            job["id"],
            instructions=creator_import_evidence.GROUP_INSTRUCTIONS
            if group
            else creator_import_evidence.PERSON_INSTRUCTIONS,
            output_format=creator_import_evidence.group_output_format()
            if group
            else creator_import_evidence.person_output_format(),
            max_output_tokens=5000,
        )
        validator = creator_import_evidence.group_validate if group else creator_import_evidence.person_validate
        _finish(job, settings, response=response, result=validator(response["text"], pack))
    except contact_assessments.OpenAIRequestError as exc:
        if exc.is_quota:
            ai_dispatch.pause_for_quota()
        if exc.retryable and not exc.is_quota and job["attempts"] < 2:
            delay = min(max(exc.retry_after_seconds or 5, 1), 3600)
            ai_dispatch.throttle(delay)
            with connection() as conn, conn.transaction():
                if _current(conn, job):
                    conn.execute(
                        """UPDATE creator_import_rows SET status='queued',reason_code='rate_limited',
                        lease_until=NULL,scheduled_at=NOW()+(%s*INTERVAL '1 second') WHERE id=%s""",
                        (delay, job["id"]),
                    )
        else:
            _finish(job, settings, response={"telemetry": exc.telemetry}, reason="provider_error")
    except (ValueError, PermissionError, TypeError, KeyError) as exc:
        _finish(job, settings, response=response, reason=str(exc) if str(exc) in REASONS else "invalid_result")
    except Exception:
        LOGGER.error("creator import AI failed row=%s", job["id"])
        _finish(job, settings, response=response, reason="provider_error")

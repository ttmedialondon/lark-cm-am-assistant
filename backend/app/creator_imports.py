"""Explicit single-CM rosters matched to the unique creator in their groups."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import PurePath
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import ai_dispatch, contact_assessments, creator_handles, creator_import_evidence
from . import creator_ownership as ownership
from .ai_feature_config import api_key_cipher
from .creator_handle_evidence import normalize_handle
from .creator_import_xlsx import MAX_FILE_BYTES, parse_xlsx  # noqa: F401 - routes share the upload bound
from .db import connection

POLICY_VERSION = "creator-import-v1"
LOGGER = logging.getLogger(__name__)
REASONS = {
    "existing": "已有相同账号和负责人，已复用正式关联",
    "classified": "匹配已完成，请核对对应用户",
    "unique_creator": "对应群内只有一位已识别达人，已匹配",
    "no_creator": "对应群内未识别到达人，用户留空待核对",
    "multiple_creators": "对应群内有多位达人，用户留空待核对",
    "group_not_found": "未找到 handle 对应群，用户留空待核对",
    "group_uncertain": "找到相似群，但尚未确定对应关系，用户留空待核对",
    "material_limit": "相关群或待识别人员较多，本次未完成核对，用户留空",
    "no_readable_material": "当前没有可访问的对应群，用户留空待核对",
    "manual_confirmed": "已确认入库",
    "no_match": "未找到对应用户，请搜索并确认",
    "insufficient_material": "材料不足，请手动确认",
    "account_conflict": "已有账号、人员类别、CAP 属性或负责人冲突，未覆盖原资料",
    "other_batch": "相同 handle 已在另一个批次分析，请等待或手动确认",
    "authorization_changed": "授权或原文已变化，请重新上传核对",
    "configuration_changed": "OpenAI 配置已变化，请重新上传核对",
    "preparation_failed": "材料准备异常，可手动确认",
    "interrupted": "执行中断，未自动重发付费请求",
    "provider_error": "OpenAI 请求异常，可手动确认",
    "invalid_result": "AI 结果不完整，请手动确认",
    "rate_limited": "等待 OpenAI 限速恢复",
}
STATUS_LABELS = {
    "pending": "等待匹配",
    "preparing": "准备材料",
    "queued": "排队中",
    "running": "分析中",
    "succeeded": "待确认",
    "skipped": "待确认",
    "failed": "分析异常",
    "invalid": "待确认",
}
BATCH_LABELS = {"preview": "等待开始", "running": "匹配中", "paused": "已暂停", "completed": "匹配完成"}


def source_labels(row):
    """Credit AI when valid group or identity analysis contributed to the result."""
    stats = row.get("stats") or {}
    ai = stats.get("ai_matched_handle") if "ai_matched_handle" in stats else (
        row.get("result_ciphertext") is not None
        and row.get("outcome") in {"matched", "review", "conflict"}
        and row.get("confidence") is not None
    )
    return ["Excel", "AI"] if ai else ["Excel"]


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
        raise ValueError("匹配规则已更新，请重新上传名单。")
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


def _admit(conn, row, actor, handle, contact_id, *, automatic=False, existing=False):
    try:
        handle = normalize_handle(handle)
    except ValueError as exc:
        raise ValueError("请填写正确的 handle。") from exc
    contact_id = ownership._uuid(contact_id)
    if not handle:
        raise ValueError("请填写正确的 handle。")
    retain_ai = automatic or (
        row["handle"] == handle and row["contact_id"] == contact_id and "AI" in source_labels(row)
    )
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
    if not existing and not _accessible(conn, row["cm_authorization_id"], contact_id):
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
            "excel_ai_verified" if automatic and "AI" in source_labels(row)
            else "excel_verified" if automatic else "cm_confirmation",
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
            "manual",
            None,
            actor,
            row["id"],
            contact_id,
        ),
    )
    _record_roster_identity(conn, row, actor, contact_id, automatic=automatic)
    conn.execute(
        """UPDATE creator_import_rows SET handle=%s,contact_id=%s,review_status='confirmed',
        status='succeeded',outcome='matched',reason_code=%s,reviewed_by=%s,reviewed_at=NOW(),lease_until=NULL,
        updated_at=NOW(),pack_ciphertext=CASE WHEN %s THEN pack_ciphertext ELSE NULL END,
        result_ciphertext=CASE WHEN %s THEN result_ciphertext ELSE NULL END,
        confidence=CASE WHEN %s THEN confidence ELSE NULL END,
        fingerprint=CASE WHEN %s THEN fingerprint ELSE NULL END,
        config_fingerprint=CASE WHEN %s THEN config_fingerprint ELSE NULL END,
        refs=CASE WHEN %s THEN refs ELSE '[]'::jsonb END,
        stats=CASE WHEN %s THEN stats ELSE '{}'::jsonb END WHERE id=%s""",
        (
            handle,
            contact_id,
            "existing" if existing else "classified" if automatic else "manual_confirmed",
            None if automatic else actor,
            retain_ai,
            retain_ai,
            retain_ai,
            retain_ai,
            retain_ai,
            retain_ai,
            retain_ai,
            row["id"],
        ),
    )
    _audit(
        conn,
        actor,
        "auto_admitted" if automatic else "confirmed",
        {"row_id": str(row["id"]), "cm_id": str(row["cm_authorization_id"])},
    )


def _record_roster_identity(conn, row, actor, contact_id, *, automatic):
    """Called only after account/person locks and all admission conflict checks."""
    roster_type = row.get("batch_creator_type", "pending")
    if roster_type != "pending":
        conn.execute("UPDATE contacts SET creator_type=%s,updated_at=NOW() WHERE id=%s", (roster_type, contact_id))
    if automatic and roster_type == "pending":
        return
    conn.execute("UPDATE contact_roles SET is_primary=FALSE WHERE contact_id=%s AND is_primary", (contact_id,))
    conn.execute(
        """INSERT INTO contact_roles(contact_id,role,status,source,is_primary,
        reviewed_by_authorization_id,reviewed_at) VALUES(%s,'creator','confirmed',%s,TRUE,%s,NOW())
        ON CONFLICT(contact_id,role) DO UPDATE SET status='confirmed',source=EXCLUDED.source,is_primary=TRUE,
        reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,reviewed_at=NOW(),updated_at=NOW()
        WHERE contact_roles.status<>'confirmed' OR EXCLUDED.source='cm_confirmation'
        OR contact_roles.source IN ('ai_auto_confirmed','lark_internal_tenant')""",
        (contact_id, "excel_ai_verified" if automatic and "AI" in source_labels(row)
         else "excel_verified" if automatic else "cm_confirmation", None if automatic else actor),
    )
    conn.execute("UPDATE contact_roles SET is_primary=TRUE WHERE contact_id=%s AND role='creator'", (contact_id,))


def _settle(conn, batch_id):
    conn.execute(
        """UPDATE creator_import_batches b SET status='completed',updated_at=NOW()
        WHERE id=%s AND status='running' AND NOT EXISTS (SELECT 1 FROM creator_import_rows r
            WHERE r.batch_id=b.id AND r.status IN ('pending','preparing','queued','running')
            AND r.review_status<>'confirmed')""",
        (batch_id,),
    )


def preview_import(actor_id, cm_id, filename, content, creator_type="cap"):
    if creator_type not in {"cap", "non_cap", "pending"}:
        raise ValueError("请选择 CAP、非 CAP 或待定。")
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
        for row in parsed["rows"]:
            if not row["error"]:
                conn.execute(
                    """INSERT INTO creator_import_rows(id,batch_id,cm_authorization_id,
                    row_number,original_handle,handle) VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                    (uuid4(), batch["id"], cm_id, row["row_number"], row["handle"], row["handle"]),
                )
        return {
            "id": str(batch["id"]),
            "status": batch["status"],
            "filename": batch["filename"],
            "creator_type": batch["creator_type"],
            "counts": parsed["counts"],
            "rows": parsed["rows"],
        }


def start_import(actor_id, batch_id):
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] == "preview":
            if not batch["counts"].get("valid"):
                raise ValueError("没有可以导入的 handle，请修改表格。")
            conn.execute(
                "UPDATE creator_import_batches SET status='running',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
            _audit(conn, ownership._uuid(actor_id), "started", {"batch_id": str(batch["id"])})
            batch["status"] = "running"
        return {"id": str(batch["id"]), "status": batch["status"]}


def pause_import(actor_id, batch_id):
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] == "running":
            conn.execute(
                "UPDATE creator_import_batches SET status='paused',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
            batch["status"] = "paused"
        return {"id": str(batch["id"]), "status": batch["status"]}


def resume_import(actor_id, batch_id):
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] == "paused":
            conn.execute(
                "UPDATE creator_import_batches SET status='running',updated_at=NOW() WHERE id=%s", (batch["id"],)
            )
            batch["status"] = "running"
            _settle(conn, batch["id"])
        return {"id": str(batch["id"]), "status": batch["status"]}


def confirm_row(actor_id, row_id, handle, contact_id):
    with connection() as conn, conn.transaction():
        row, batch = _row(conn, actor_id, row_id)
        if batch["status"] == "preview":
            raise ValueError("请先确认开始导入。")
        _admit(conn, row, ownership._uuid(actor_id), handle, contact_id)
        _settle(conn, row["batch_id"])
        return {"id": str(row["id"]), "status": "confirmed"}


def defer_row(actor_id, row_id):
    with connection() as conn, conn.transaction():
        row, _ = _row(conn, actor_id, row_id)
        if row["review_status"] == "confirmed":
            raise ValueError("已入库的记录不能暂缓。")
        conn.execute(
            """UPDATE creator_import_rows SET review_status='deferred',status='skipped',lease_until=NULL,
            pack_ciphertext=NULL,result_ciphertext=NULL,updated_at=NOW() WHERE id=%s""",
            (row["id"],),
        )
        _settle(conn, row["batch_id"])
        return {"id": str(row["id"]), "status": "deferred"}


def recheck_batch(actor_id, batch_id):
    """Rebuild pending matches under current rules, preserving formal data and paid usage."""
    with connection() as conn, conn.transaction():
        batch = _batch(conn, actor_id, batch_id)
        if batch["status"] not in {"completed", "paused"} or conn.execute(
            "SELECT 1 FROM creator_import_rows WHERE batch_id=%s AND status IN ('preparing','queued','running')",
            (batch["id"],),
        ).fetchone():
            raise ValueError("请等待本批匹配结束后再重新匹配。")
        rows = conn.execute(
            "SELECT * FROM creator_import_rows WHERE batch_id=%s AND review_status='pending' FOR UPDATE",
            (batch["id"],),
        ).fetchall()
        for row in rows:
            _audit(conn, ownership._uuid(actor_id), "reanalysis_requested", {
                "row_id": str(row["id"]), "handle": row["handle"],
                "previous": {key: row[key] for key in (
                    "pack_ciphertext", "result_ciphertext", "refs", "fingerprint", "stats", "request_id", "response_id",
                )},
            })
            conn.execute(
                """UPDATE creator_import_rows SET status='pending',reason_code='',outcome=NULL,contact_id=NULL,
                pack_ciphertext=NULL,result_ciphertext=NULL,refs='[]',fingerprint=NULL,config_fingerprint=NULL,
                confidence=NULL,stats='{}',attempts=0,lease_until=NULL,request_id=NULL,response_id=NULL,
                scheduled_at=NOW(),updated_at=NOW() WHERE id=%s""", (row["id"],),
            )
        if rows:
            conn.execute(
                "UPDATE creator_import_batches SET status='running',updated_at=NOW() WHERE id=%s", (batch["id"],),
            )
        return {"id": str(batch["id"]), "queued": len(rows)}


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


def _pack(row, settings, key="pack_ciphertext"):
    return json.loads(ownership._cipher(settings).decrypt(row[key]))


def _valid(conn, row, settings):
    pack = _pack(row, settings)
    return creator_import_evidence.validate_evidence(
        conn, settings, row["cm_authorization_id"], row["handle"], pack, row["refs"], row["fingerprint"],
        creator_type=row["batch_creator_type"],
    )


def _refresh_known_result(conn, row, settings, *, response=None):
    """Another handle may have classified the same member while this row was queued."""
    material = creator_import_evidence.build_evidence(
        conn, settings, row["cm_authorization_id"], row["handle"], creator_type=row["batch_creator_type"],
    )
    if material["checked"] is None:
        return False
    conn.execute(
        """UPDATE creator_import_rows SET pack_ciphertext=%s,refs=%s,fingerprint=%s,stats=%s WHERE id=%s""",
        (ownership._cipher(settings).encrypt(ownership._encode(material["pack"])), Jsonb(material["refs"]),
         material["fingerprint"], Jsonb(material["stats"]), row["id"]),
    )
    original = ({"pack": _pack(row, settings), "refs": row["refs"], "fingerprint": row["fingerprint"]}
                if response else None)
    row.update(refs=material["refs"], stats=material["stats"])
    _apply_result(conn, row, material["checked"], settings, response=response, response_evidence=original)
    _settle(conn, row["batch_id"])
    return True


def _apply_result(conn, row, result, settings, *, response=None, response_evidence=None):
    """One admission path for existing classifications, cached results and fresh identity AI."""
    contact_id = creator_import_evidence.resolve_contact(row["refs"], result.get("user_ref"))
    stats = {"ai_matched_handle": result["ai_used"], "creator_count": result["creator_count"],
             "matched_group_count": result["matched_group_count"], "group_match_kind": result["group_match_kind"]}
    conn.execute(
        """UPDATE creator_import_rows SET status='succeeded',reason_code=%s,result_ciphertext=%s,
        contact_id=%s,confidence=%s,outcome=%s,stats=stats||%s::jsonb,lease_until=NULL,updated_at=NOW() WHERE id=%s""",
        (result["reason_code"], ownership._cipher(settings).encrypt(ownership._encode({
            "response": response, "checked": result, "response_evidence": response_evidence,
        })), contact_id, result["confidence"], result["outcome"], Jsonb(stats), row["id"]),
    )
    if result["outcome"] == "matched" and contact_id:
        try:
            with conn.transaction():
                _admit(conn, {**row, "stats": {**row["stats"], **stats}}, row["uploaded_by"], row["handle"],
                       contact_id, automatic=True)
        except (ValueError, PermissionError):
            conn.execute(
                "UPDATE creator_import_rows SET outcome='conflict',reason_code='account_conflict' WHERE id=%s",
                (row["id"],),
            )


def prepare_next_import(settings):
    # One free-material worker serializes batches; paid calls still share the global parallel queue.
    with connection() as conn, conn.transaction():
        batch = conn.execute(
            """SELECT b.id FROM creator_import_batches b WHERE b.status='running'
            AND b.policy_version=%s AND EXISTS (SELECT 1 FROM creator_import_rows r WHERE r.batch_id=b.id
                AND r.status='pending' AND r.review_status='pending')
            ORDER BY b.created_at,b.id FOR UPDATE OF b SKIP LOCKED LIMIT 1""",
            (POLICY_VERSION,),
        ).fetchone()
        if not batch:
            return False
        row = conn.execute(
            """SELECT r.*,b.uploaded_by,b.creator_type batch_creator_type FROM creator_import_rows r
            JOIN creator_import_batches b ON b.id=r.batch_id WHERE b.status='running' AND b.policy_version=%s
            AND r.batch_id=%s AND r.status='pending' AND r.review_status='pending'
            ORDER BY r.created_at,r.id FOR UPDATE OF r SKIP LOCKED LIMIT 1""",
            (POLICY_VERSION, batch["id"]),
        ).fetchone()
        if not row:
            return False
        conn.execute("SAVEPOINT import_prepare")
        try:
            creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
            contact_id = _existing(conn, row)
            if contact_id:
                _admit(conn, row, row["uploaded_by"], row["handle"], contact_id, automatic=True, existing=True)
                _settle(conn, row["batch_id"])
                return True
            material = creator_import_evidence.build_evidence(
                conn, settings, row["cm_authorization_id"], row["handle"], creator_type=row["batch_creator_type"],
            )
            config = ownership._config(conn) if material["eligible"] else None
            if material["eligible"] and not config:
                raise ValueError("configuration_changed")
            duplicate = conn.execute(
                """SELECT 1 FROM creator_import_rows WHERE cm_authorization_id=%s AND handle=%s
                AND id<>%s AND status IN ('queued','running')""",
                (row["cm_authorization_id"], row["handle"], row["id"]),
            ).fetchone()
            status = "queued" if material["eligible"] and not duplicate else "skipped"
            reason = "other_batch" if duplicate else "" if material["eligible"] else material["reason_code"]
            conn.execute(
                """UPDATE creator_import_rows SET status=%s,reason_code=%s,pack_ciphertext=%s,
                refs=%s,fingerprint=%s,config_fingerprint=%s,stats=%s,updated_at=NOW() WHERE id=%s""",
                (
                    status,
                    reason,
                    ownership._cipher(settings).encrypt(ownership._encode(material["pack"])),
                    Jsonb(material["refs"]),
                    material["fingerprint"],
                    ownership._config_fingerprint(config) if config else None,
                    Jsonb(material["stats"]),
                    row["id"],
                ),
            )
            row.update(refs=material["refs"], stats=material["stats"])
            if material["checked"] is not None:
                _apply_result(conn, row, material["checked"], settings)
            elif status == "queued":
                cached = conn.execute(
                    """SELECT result_ciphertext FROM creator_import_rows
                    WHERE cm_authorization_id=%s AND handle=%s AND status='succeeded' AND result_ciphertext IS NOT NULL
                    AND fingerprint=%s AND config_fingerprint=%s ORDER BY updated_at DESC LIMIT 1""",
                    (row["cm_authorization_id"], row["handle"], material["fingerprint"],
                     ownership._config_fingerprint(config)),
                ).fetchone()
                if cached:
                    response = _pack(cached, settings, "result_ciphertext")["response"]
                    if response:
                        result = creator_import_evidence.validate_result(response["text"], material["pack"])
                        _apply_result(conn, row, result, settings, response=response)
        except (ValueError, PermissionError) as exc:
            conn.execute("ROLLBACK TO SAVEPOINT import_prepare")
            reason = (
                "account_conflict"
                if str(exc) == REASONS["account_conflict"]
                else "configuration_changed"
                if str(exc) == "configuration_changed"
                else "authorization_changed"
            )
            conn.execute(
                "UPDATE creator_import_rows SET status='invalid',reason_code=%s WHERE id=%s", (reason, row["id"])
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT import_prepare")
            LOGGER.warning("creator import material failed row=%s", row["id"])
            conn.execute(
                """UPDATE creator_import_rows SET status='failed',reason_code='preparation_failed'
                WHERE id=%s""",
                (row["id"],),
            )
        finally:
            conn.execute("RELEASE SAVEPOINT import_prepare")
        _settle(conn, row["batch_id"])
        return True


def claim_next_import():
    with connection() as conn, conn.transaction():
        expired = conn.execute("""SELECT DISTINCT batch_id FROM creator_import_rows
            WHERE status='running' AND lease_until<NOW() ORDER BY batch_id""").fetchall()
        for row in expired:
            if not conn.execute(
                "SELECT id FROM creator_import_batches WHERE id=%s FOR UPDATE SKIP LOCKED", (row["batch_id"],)
            ).fetchone():
                continue
            conn.execute(
                """UPDATE creator_import_rows SET status='failed',reason_code='interrupted',
                lease_until=NULL,updated_at=NOW() WHERE batch_id=%s AND status='running' AND lease_until<NOW()""",
                (row["batch_id"],),
            )
            _settle(conn, row["batch_id"])
        if not ai_dispatch.can_claim(conn):
            return None
        row = conn.execute(
            """SELECT r.*,b.uploaded_by FROM creator_import_rows r
            JOIN creator_import_batches b ON b.id=r.batch_id WHERE b.status='running' AND b.policy_version=%s
            AND r.status='queued' AND r.review_status='pending' AND r.attempts<2 AND r.scheduled_at<=NOW()
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
    return conn.execute(
        """SELECT r.*,b.status batch_status,b.uploaded_by,b.creator_type batch_creator_type FROM creator_import_rows r
        JOIN creator_import_batches b ON b.id=r.batch_id WHERE r.id=%s AND r.status='running'
        AND r.attempts=%s AND r.review_status='pending' FOR UPDATE OF r""",
        (job["id"], job["attempts"]),
    ).fetchone()


def _finish(job, status, reason, settings, response=None, result=None):
    with connection() as conn, conn.transaction():
        row = _current(conn, job)
        if not row:
            return
        refresh = False
        if result:
            try:
                creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
                config = ownership._config(conn)
                if (
                    not config
                    or ownership._config_fingerprint(config) != row["config_fingerprint"]
                ):
                    raise ValueError("配置变化")
                if not _valid(conn, row, settings):
                    refresh = True
                    status, reason, result = "invalid", "authorization_changed", None
            except (ValueError, PermissionError):
                status, reason, result = "invalid", "authorization_changed", None
        usage = (response or {}).get("usage", {})
        conn.execute(
            """UPDATE creator_import_rows SET status=%s,reason_code=%s,result_ciphertext=%s,
            outcome=NULL,contact_id=NULL,confidence=NULL,input_tokens=input_tokens+%s,output_tokens=output_tokens+%s,
            response_id=%s,request_id=%s,stats=stats||'{"ai_matched_handle":false}'::jsonb,
            lease_until=NULL,updated_at=NOW() WHERE id=%s""",
            (status, reason,
             ownership._cipher(settings).encrypt(ownership._encode({"response": response, "checked": None}))
             if response else None,
             usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
             (response or {}).get("response_id"), (response or {}).get("telemetry", {}).get("openai_request_id"),
             row["id"]),
        )
        if result:
            _apply_result(conn, row, result, settings, response=response)
        elif refresh:
            _refresh_known_result(conn, row, settings, response=response)
        _settle(conn, row["batch_id"])


def run_claimed_import(job, settings):
    response = None
    try:
        with connection() as conn, conn.transaction():
            row = _current(conn, job)
            if not row:
                return
            creator_handles._access(conn, row["uploaded_by"], row["cm_authorization_id"])
            if row["batch_status"] == "paused":
                conn.execute(
                    "UPDATE creator_import_rows SET status='queued',attempts=attempts-1,lease_until=NULL WHERE id=%s",
                    (row["id"],),
                )
                return
            if row["batch_status"] != "running":
                raise ValueError("批次状态已变化")
            if not _valid(conn, row, settings):
                if _refresh_known_result(conn, row, settings):
                    return
                raise ValueError("材料已变化")
            config = ownership._config(conn)
            if not config or ownership._config_fingerprint(config) != row["config_fingerprint"]:
                raise ValueError("配置已变化")
            pack = _pack(row, settings)
            key = api_key_cipher(settings.token_encryption_key).decrypt(config["api_key_ciphertext"])
            conn.execute("UPDATE creator_import_rows SET api_requests=api_requests+1 WHERE id=%s", (row["id"],))
        response = contact_assessments.call_openai(
            config,
            key,
            pack,
            job["id"],
            instructions=creator_import_evidence.INSTRUCTIONS,
            output_format=creator_import_evidence.output_format(),
            max_output_tokens=4000,
        )
        result = creator_import_evidence.validate_result(response["text"], pack)
        _finish(job, "succeeded", "classified", settings, response, result)
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
                        lease_until=NULL,scheduled_at=NOW()+(%s*INTERVAL '1 second'),request_id=%s WHERE id=%s""",
                        (delay, exc.telemetry.get("openai_request_id"), job["id"]),
                    )
        else:
            _finish(job, "failed", "provider_error", settings, {"telemetry": exc.telemetry})
    except (ValueError, PermissionError, TypeError, KeyError):
        _finish(job, "invalid", "invalid_result" if response else "authorization_changed", settings, response)
    except Exception:
        LOGGER.warning("creator import request failed row=%s", job["id"])
        _finish(job, "failed", "provider_error", settings, response)


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
                "can_pause": batch["status"] == "running",
                "can_resume": batch["status"] == "paused",
            }
        )
    return decorated


def _review_reason(row, visible):
    if row["contact_id"] and not visible:
        return "对应用户当前不可访问，请重新选择可访问的 Lark 用户。"
    if row["reason_code"] == "multiple_creators":
        return f"对应群合并去重后有 {row['stats']['creator_count']} 位达人，用户留空待核对。"
    return REASONS.get(row["reason_code"], "请核对 handle 和 Lark 用户。")


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
            else "匹配完成，待核对" if r["status"] == "succeeded" else STATUS_LABELS[r["status"]],
            "reason": _review_reason(r, r["contact_id"] in visible_ids),
            "confidence": float(r["confidence"])
            if r["confidence"] is not None and r["contact_id"] in visible_ids else None,
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

"""Historical handle evidence and manual review; automated scanning is retired."""

from __future__ import annotations

import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import creator_access, creator_data
from . import creator_ownership as ownership
from .config import get_settings
from .db import connection

POLICY_VERSION = "creator-handles-v1"
REASONS = {
    **ownership.REASONS,
    "parent_changed": "第二步的归属建议已变化，请重新核对负责人。",
    "prepared": "材料已准备，尚未调用 AI。",
    "classified": "已完成账号匹配。",
    "no_handle_material": "没有找到明确的 handle 线索，可手动填写。",
    "no_handle_clues": "没有找到明确的 handle 线索，可手动填写。",
    "no_handle_clue": "没有找到明确的 handle 线索，可手动填写。",
    "not_found": "没有找到可确认的 handle，可手动填写。",
    "account_conflict": "账号与人员关联存在冲突，未覆盖已有记录。",
}


def _audit(conn, actor, action, metadata):
    conn.execute(
        "INSERT INTO audit_events(id,authorization_id,action,metadata) VALUES(%s,%s,%s,%s)",
        (uuid4(), actor, "creator_handles_" + action, Jsonb(metadata)),
    )


def _run(conn, actor, run_id, *, manage=False):
    row = conn.execute(
        "SELECT * FROM creator_handle_runs WHERE id=%s FOR UPDATE", (creator_access.parse_id(run_id),)
    ).fetchone()
    if not row:
        raise LookupError("找不到该批次。")
    creator_access.access(conn, actor, row["cm_authorization_id"], manage=manage)
    if row["policy_version"] != POLICY_VERSION:
        raise ValueError("该批次的账号匹配规则已更新，请重新核对。")
    return row


def _task(conn, actor, task_id):
    task_id = creator_access.parse_id(task_id)
    row = conn.execute("SELECT run_id FROM creator_handle_tasks WHERE id=%s", (task_id,)).fetchone()
    if not row:
        raise LookupError("找不到该候选。")
    run = _run(conn, actor, row["run_id"])
    task = conn.execute("SELECT * FROM creator_handle_tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
    if task["policy_version"] != POLICY_VERSION:
        raise ValueError("该候选的匹配规则已更新。")
    return task, run


def _parent(conn, task, settings, *, full=True):
    parent = conn.execute(
        "SELECT * FROM creator_ownership_tasks WHERE id=%s FOR SHARE", (task["ownership_task_id"],)
    ).fetchone()
    if (
        not parent
        or parent["contact_id"] != task["contact_id"]
        or parent["cm_authorization_id"] != task["cm_authorization_id"]
        or parent["policy_version"] != ownership.POLICY_VERSION
        or parent["status"] != "succeeded"
        or parent["outcome"] != "suggested"
        or parent["confidence"] is None
        or parent["confidence"] < 0.95
    ):
        raise ValueError(REASONS["parent_changed"])
    newer = conn.execute(
        """SELECT 1 FROM creator_ownership_tasks WHERE contact_id=%s
        AND cm_authorization_id=%s AND status='succeeded' AND (created_at,id)>(%s,%s) LIMIT 1""",
        (task["contact_id"], task["cm_authorization_id"], parent["created_at"], parent["id"]),
    ).fetchone()
    if (
        newer
        or not ownership._creator(conn, task["contact_id"])
        or not creator_access.authorized_cm(conn, task["cm_authorization_id"])
    ):
        raise ValueError(REASONS["parent_changed"])
    if not ownership._valid_material(conn, parent, settings, check_history=full):
        raise ValueError(REASONS["parent_changed"])
    return parent


def _decode(task, key, settings):
    value = json.loads(creator_data.cipher(settings).decrypt(task[key])) if task.get(key) else {}
    if key == "result_ciphertext" and "checked" in value:
        return value["checked"] or {}
    return value


def _valid_handle_material(task, settings, *, full=True):
    from .creator_handle_evidence import validate_evidence

    return bool(
        task.get("pack_ciphertext")
        and validate_evidence(
            task["contact_id"],
            task["cm_authorization_id"],
            settings,
            _decode(task, "pack_ciphertext", settings),
            task["refs"],
            task["fingerprint"],
            check_history=full,
        )
    )


def _conflict(conn, task, handles):
    return bool(
        conn.execute(
            """SELECT 1 FROM social_accounts s WHERE s.platform='tiktok'
        AND lower(s.normalized_handle)=ANY(%s::text[]) AND (
          EXISTS (SELECT 1 FROM contact_social_accounts l WHERE l.social_account_id=s.id
            AND l.status='confirmed' AND (l.contact_id<>%s OR l.relationship<>'owner'))
          OR EXISTS (SELECT 1 FROM contact_social_accounts l WHERE l.social_account_id=s.id
            AND l.contact_id=%s AND l.status='rejected')
          OR EXISTS (SELECT 1 FROM creator_account_assignments a WHERE a.social_account_id=s.id
            AND a.ended_at IS NULL AND a.contact_id<>%s)) LIMIT 1""",
            (handles, task["contact_id"], task["contact_id"], task["contact_id"]),
        ).fetchone()
    )


def _admit(conn, task, handles, actor, settings):
    from .creator_handle_evidence import normalize_handle

    if not isinstance(handles, list) or not 1 <= len(handles) <= 20 or any(not isinstance(v, str) for v in handles):
        raise ValueError("请填写或选择 1–20 个明确的 handle。")
    handles = sorted({normalize_handle(v) for v in handles})
    if not all(handles):
        raise ValueError("handle 格式不正确。")
    if task["review_status"] == "confirmed":
        if handles == sorted(task["selected_handles"]):
            return 0
        raise ValueError("该候选已经入库，不能用重复请求覆盖已确认账号。")
    _parent(conn, task, settings)
    if task["status"] in ("pending", "preparing", "ready", "queued", "running"):
        raise ValueError("请等待本次取材或分析完成。")
    # All writers use sorted account locks, then the person lock. No account can be silently stolen.
    for handle in handles:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,137034))", ("tiktok:" + handle,))
    conn.execute("SELECT id FROM contacts WHERE id=%s FOR UPDATE", (task["contact_id"],))
    # A newer ownership result may have committed while we waited for the person lock.
    _parent(conn, task, settings, full=False)
    if _conflict(conn, task, handles):
        raise ValueError(REASONS["account_conflict"])
    source = "cm_confirmation"
    for handle in handles:
        account = conn.execute(
            """SELECT id FROM social_accounts WHERE platform='tiktok'
            AND lower(normalized_handle)=%s FOR UPDATE""",
            (handle,),
        ).fetchall()
        if len(account) > 1:
            raise ValueError(REASONS["account_conflict"])
        account = (
            account[0]
            if account
            else conn.execute(
                """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
            VALUES(%s,'tiktok',%s,%s) ON CONFLICT(platform,normalized_handle) DO UPDATE
            SET normalized_handle=EXCLUDED.normalized_handle RETURNING id""",
                (uuid4(), handle, handle),
            ).fetchone()
        )
        conn.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,
            reviewed_by_authorization_id,reviewed_at,handle_task_id) VALUES(%s,%s,'owner','confirmed',%s,%s,NOW(),%s)
            ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET
            handle_task_id=COALESCE(contact_social_accounts.handle_task_id,EXCLUDED.handle_task_id)
            WHERE contact_social_accounts.status='confirmed'""",
            (task["contact_id"], account["id"], source, actor, task["id"]),
        )
    conn.execute(
        """INSERT INTO contact_assignments(id,contact_id,responsible_authorization_id,source,confidence,
        created_by_authorization_id,ownership_task_id,handle_task_id) SELECT %s,%s,%s,%s,%s,%s,%s,%s
        WHERE NOT EXISTS (SELECT 1 FROM contact_assignments WHERE contact_id=%s
          AND responsible_authorization_id=%s AND ended_at IS NULL)
        ON CONFLICT (contact_id,responsible_authorization_id) WHERE ended_at IS NULL DO NOTHING""",
        (
            uuid4(),
            task["contact_id"],
            task["cm_authorization_id"],
            "manual",
            None,
            actor,
            task["ownership_task_id"],
            task["id"],
            task["contact_id"],
            task["cm_authorization_id"],
        ),
    )
    conn.execute(
        """UPDATE creator_handle_tasks SET review_status='confirmed',selected_handles=%s,
        reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s""",
        (handles, actor, task["id"]),
    )
    _audit(
        conn,
        actor,
        "confirmed",
        {"task_id": str(task["id"]), "cm_id": str(task["cm_authorization_id"]), "accounts": len(handles)},
    )
    return 1


def confirm_task(actor_id, task_id, handles):
    actor_id = creator_access.parse_id(actor_id)
    with connection() as conn, conn.transaction():
        task, _ = _task(conn, actor_id, task_id)
        return _admit(conn, task, handles, actor_id, get_settings())


def defer_task(actor_id, task_id):
    actor_id = creator_access.parse_id(actor_id)
    with connection() as conn, conn.transaction():
        task, _ = _task(conn, actor_id, task_id)
        if task["review_status"] == "confirmed":
            raise ValueError("已入库的达人不能暂缓。")
        conn.execute(
            "UPDATE creator_handle_tasks SET review_status='deferred',updated_at=NOW() WHERE id=%s", (task["id"],)
        )
        _audit(conn, actor_id, "deferred", {"task_id": str(task["id"])})


def task_evidence(actor_id, task_id):
    settings = get_settings()
    with connection() as conn, conn.transaction():
        task, _ = _task(conn, actor_id, task_id)
        _parent(conn, task, settings)
        if not _valid_handle_material(task, settings):
            raise ValueError("账号匹配材料已变化，请重新核对。")
        pack = _decode(task, "pack_ciphertext", settings)
        result = _decode(task, "result_ciphertext", settings)
        person = conn.execute("SELECT display_name FROM contacts WHERE id=%s", (task["contact_id"],)).fetchone()
        cm = creator_access.authorized_cm(conn, task["cm_authorization_id"])
        if not person or not cm:
            raise ValueError("人员或负责人授权已变化，请重新核对。")
        messages = []
        for group in pack.get("groups", []):
            messages.append(
                {
                    "ref": group.get("ref"),
                    "sender_name": "群名",
                    "time_label": "当前观察值",
                    "chat_name": "",
                    "text": group.get("name") or group.get("title") or "—",
                    "available": True,
                }
            )
        for message in pack.get("messages", []):
            messages.append(
                {
                    "ref": message.get("ref"),
                    "sender_name": person["display_name"]
                    if message.get("speaker") == "U1"
                    else cm["display_name"]
                    if message.get("speaker") == "CM1"
                    else "其他成员",
                    "time_label": message.get("time", ""),
                    "chat_name": message.get("conversation", ""),
                    "text": message.get("text", ""),
                    "available": True,
                }
            )
        _audit(conn, creator_access.parse_id(actor_id), "evidence_viewed", {"task_id": str(task["id"])})
        return {
            "display_name": person["display_name"],
            "cm_name": cm["display_name"],
            "summary": result.get("reason", "账号匹配材料"),
            "messages": messages,
        }

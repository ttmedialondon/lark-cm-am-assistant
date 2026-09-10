"""On-demand evidence reads: candidate links never replace live chat authorization."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag

from . import creator_workflow as workflow
from .admin_reporting import _format_time, _message_type_label, _readable_body
from .config import get_settings
from .crypto import message_body_cipher
from .db import connection
from .message_mentions import mention_names

NOTICE = "只显示这位 CM 当前有权查看、与该用户有关的已同步会话。私聊仅作辅助依据，不会自动确认归属。"


def _text_content(
    ciphertext: str | None, cipher: Any, message_type: str | None = "text", mentions=None, names=None
) -> str:
    if ciphertext is None:
        return "（正文尚未同步）"
    try:
        text = _readable_body(cipher.decrypt(ciphertext), _message_type_label(message_type), mentions, names)
    except (InvalidTag, UnicodeDecodeError, ValueError, TypeError):
        return "（正文无法读取）"
    text = re.sub(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()\-]{6,}\d(?!\w)", "[电话号码已隐藏]", text)
    return re.sub(r"\b(?:ou|on|cli)_[A-Za-z0-9]+\b", "[标识已隐藏]", text)


def evidence(scope, candidate_id: UUID, chat_id: str | None = None, page: int = 1):
    page = max(1, min(page, 10000))
    with connection() as conn, conn.transaction():
        # Hold the viewer's authorization and owner role while reading so
        # concurrent revocation cannot leave stale access in this response.
        actor_id = UUID(scope["actor_id"])
        conn.execute("SELECT id FROM authorizations WHERE id=%s FOR SHARE", (actor_id,)).fetchall()
        conn.execute(
            "SELECT authorization_id FROM admin_roles WHERE authorization_id=%s FOR SHARE", (actor_id,)
        ).fetchall()
        scope = workflow._fresh(conn, scope)
        row = workflow._candidate(conn, candidate_id, scope, lock=True)
        responsible = row["responsible_authorization_id"]
        record = workflow._record(conn, row, scope)
        contact_id = record["contact_id"]
        source_refs = []
        for source in record["evidence"]:
            refs = source["source_data"].get("evidence")
            if not refs:
                continue
            try:
                source_refs.extend(workflow.validate_evidence(conn, responsible, refs))
            except (workflow.WorkflowInputError, PermissionError):
                # Other still-valid sources can remain readable; invalid sources
                # never expand the allowed chat set.
                continue
        if responsible:
            conn.execute("SELECT id FROM authorizations WHERE id=%s FOR SHARE", (responsible,)).fetchall()
            conn.execute(
                "SELECT chat_id FROM authorization_chats WHERE authorization_id=%s ORDER BY chat_id FOR SHARE",
                (responsible,),
            ).fetchall()
            if contact_id:
                conn.execute(
                    "SELECT open_id FROM lark_identities WHERE contact_id=%s ORDER BY open_id FOR SHARE",
                    (contact_id,),
                ).fetchall()
            chats = conn.execute(
                """SELECT DISTINCT c.chat_id,c.name,c.chat_mode
            FROM authorizations a JOIN authorization_chats ac ON ac.authorization_id=a.id
            JOIN chats c ON c.chat_id=ac.chat_id
            WHERE a.id=%s AND a.status='active' AND a.body_consent_at IS NOT NULL
              AND ac.is_visible AND ac.sync_enabled AND COALESCE(c.chat_status,'normal')='normal'
              AND (c.chat_id=ANY(%s::text[]) OR (%s::uuid IS NOT NULL AND (
                EXISTS(SELECT 1 FROM lark_identities i WHERE i.contact_id=%s
                    AND i.identity_type='user' AND i.open_id=ac.p2p_counterpart_open_id
                    AND ac.p2p_counterpart_type='user')
                OR EXISTS(SELECT 1 FROM chat_members m JOIN lark_identities i USING(open_id)
                    WHERE m.chat_id=c.chat_id AND i.contact_id=%s AND i.identity_type='user'
                      AND m.last_observed_by_authorization_id=a.id AND m.member_type='user')
                OR EXISTS(SELECT 1 FROM authorization_message_observations o
                    JOIN chat_messages msg ON msg.chat_id=o.chat_id AND msg.message_id=o.message_id
                    JOIN lark_identities i ON i.open_id=msg.sender_open_id
                    WHERE o.authorization_id=a.id AND o.chat_id=c.chat_id AND o.observed_at>=a.body_consent_at
                      AND i.contact_id=%s AND NOT msg.is_deleted AND i.identity_type='user')
              ))) ORDER BY c.name NULLS LAST,c.chat_id""",
                (responsible, [r["chat_id"] for r in source_refs], contact_id, contact_id, contact_id, contact_id),
            ).fetchall()
            allowed = [{"authorization_id": str(responsible), "chat_id": chat["chat_id"]} for chat in chats]
        else:
            if not scope["is_owner"]:
                raise PermissionError("负责人不明的来源证据仅供系统主人核对。")
            allowed = [
                dict(authorization_id=auth_id, chat_id=source_chat)
                for auth_id, source_chat in sorted({(ref["authorization_id"], ref["chat_id"]) for ref in source_refs})
            ]
            # No CM means no person-wide lookup. Only the exact, revalidated
            # source (authorization, chat) pairs are available to the owner.
            chats = conn.execute(
                """SELECT DISTINCT c.chat_id,c.name,c.chat_mode
                   FROM jsonb_to_recordset(%s::jsonb) AS allowed(authorization_id uuid,chat_id text)
                   JOIN authorizations a ON a.id=allowed.authorization_id
                   JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=allowed.chat_id
                   JOIN chats c ON c.chat_id=ac.chat_id
                   WHERE a.status='active' AND a.body_consent_at IS NOT NULL
                     AND ac.is_visible AND ac.sync_enabled AND COALESCE(c.chat_status,'normal')='normal'
                   ORDER BY c.name NULLS LAST,c.chat_id""",
                (json.dumps(allowed),),
            ).fetchall()
        result = {"conversations": chats, "messages": [], "notice": NOTICE, "page": page, "pages": 1}
        if not chat_id:
            return result
        if chat_id not in {chat["chat_id"] for chat in chats}:
            raise PermissionError("此会话不在当前允许查看的证据范围。")
        message_scope = """FROM chat_messages m
            WHERE m.chat_id=%s AND NOT m.is_deleted AND EXISTS (
              SELECT 1 FROM jsonb_to_recordset(%s::jsonb) AS allowed(authorization_id uuid,chat_id text)
              JOIN authorizations a ON a.id=allowed.authorization_id
              JOIN authorization_chats ac ON ac.authorization_id=a.id AND ac.chat_id=allowed.chat_id
              JOIN authorization_message_observations o ON o.authorization_id=a.id AND o.chat_id=ac.chat_id
              JOIN chats c ON c.chat_id=ac.chat_id
              WHERE o.chat_id=m.chat_id AND o.message_id=m.message_id
                AND a.status='active' AND a.body_consent_at IS NOT NULL AND o.observed_at>=a.body_consent_at
                AND ac.is_visible AND ac.sync_enabled AND COALESCE(c.chat_status,'normal')='normal'
            )"""
        params = (chat_id, json.dumps([pair for pair in allowed if pair["chat_id"] == chat_id]))
        total = conn.execute("SELECT COUNT(*) AS total " + message_scope, params).fetchone()["total"]
        rows = conn.execute(
            """SELECT m.message_id,m.message_type,m.message_create_time,m.body_ciphertext,m.mentions_json,
               (SELECT i.display_name FROM lark_identities i WHERE i.open_id=m.sender_open_id) AS sender_name """
            + message_scope
            + " ORDER BY m.message_create_time DESC NULLS LAST,m.message_id DESC LIMIT 30 OFFSET %s FOR SHARE OF m",
            (*params, (page - 1) * 30),
        ).fetchall()
        names = mention_names(conn, rows)
        cipher = message_body_cipher(get_settings().token_encryption_key)
        messages = []
        for message in rows:
            messages.append(
                {
                    "message_id": message["message_id"],
                    "time": _format_time(message["message_create_time"]),
                    "sender_name": message["sender_name"],
                    "text": _text_content(message["body_ciphertext"], cipher, message["message_type"],
                                          message.get("mentions_json"), names),
                }
            )
        result.update(messages=messages, pages=max(1, (total + 29) // 30))
        workflow._audit(
            conn,
            UUID(scope["actor_id"]),
            "evidence_read",
            {
                "candidate_id": candidate_id,
                "responsible_id": responsible,
                "chat_id": chat_id,
                "page": page,
                "message_count": len(messages),
            },
        )
        return result

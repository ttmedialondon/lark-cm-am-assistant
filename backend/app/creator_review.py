"""Owner preview of creator assignments and their authorized conversation history."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag

from .admin_reporting import (
    _format_time,
    _message_time,
    _message_type_label,
    _page_result,
    _page_window,
    _readable_body,
)
from .config import get_settings
from .crypto import message_body_cipher
from .db import connection

PAGE_SIZE = 20
MESSAGE_PAGE_SIZE = 30
CONVERSATION_NOTICE = (
    "左侧汇总此达人身份关联、当前授权范围内的全部已同步会话，不限于导入证据或当前负责人。"
    "打开会话可查看已同步的各方记录；归属证据是否完整不影响人工查阅，单聊仍只作辅助材料。"
    "未同步或已删除的正文不可查看；图片、附件等保留消息类型提示，不提供原件或跳转读取其他会话。"
    "已遮盖常见邮箱和电话号码，但自由文本仍可能提及其他人或业务信息。"
)

_CREATORS_SQL = """
    SELECT contact.id, contact.display_name, role.source AS role_source,
           role.reviewed_at, role.updated_at AS role_updated_at,
           assignment.id AS assignment_id, assignment.source AS assignment_source,
           assignment.started_at, responsible.id AS responsible_id,
           responsible.display_name AS responsible_name,
           role_audits.records AS role_audits,
           assignment_audit.id AS assignment_audit_id,
           assignment_audit.metadata AS assignment_audit
    FROM contacts contact
    JOIN contact_roles role ON role.contact_id = contact.id
      AND role.role = 'creator' AND role.status = 'confirmed' AND role.is_primary
    LEFT JOIN contact_assignments assignment
      ON assignment.contact_id = contact.id AND assignment.ended_at IS NULL
    LEFT JOIN authorizations responsible ON responsible.id = assignment.responsible_authorization_id
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(audit.metadata ORDER BY audit.created_at DESC, audit.id) AS records
        FROM audit_events audit
        WHERE audit.metadata ->> 'contact_id' = contact.id::text
          AND audit.created_at >= role.created_at
          AND audit.action = CASE role.source
              WHEN 'manual_excel_and_identity_confirmed' THEN 'contact_role_manually_confirmed_from_excel_or_group'
              WHEN 'manual_excel_import' THEN 'contact_role_manual_excel_imported'
              WHEN 'manual_p2p_confirmed' THEN 'contact_role_manually_confirmed_from_p2p'
          END
    ) role_audits ON TRUE
    LEFT JOIN LATERAL (
        SELECT audit.id, audit.metadata
        FROM audit_events audit
        WHERE audit.action = 'contact_assignment_manually_set'
          AND audit.metadata ->> 'contact_id' = contact.id::text
          AND audit.metadata ->> 'responsible_authorization_id' = responsible.id::text
          AND audit.created_at >= assignment.started_at
        ORDER BY audit.created_at DESC, audit.id DESC LIMIT 1
    ) assignment_audit ON TRUE
    WHERE contact.entity_type = 'person'
      AND NOT EXISTS (
          SELECT 1 FROM lark_identities internal
          WHERE internal.contact_id = contact.id AND internal.tenant_classification = 'internal'
      )
"""

# Scope is independent of import evidence/assignment. Exact identity links, not
# handle/name similarity, bind every read to the creator and live source consent.
_CONVERSATION_SCOPE_SQL = """
    FROM contacts target
    JOIN contact_roles role ON role.contact_id = target.id
      AND role.role = 'creator' AND role.status = 'confirmed' AND role.is_primary
    JOIN authorizations source ON source.status = 'active' AND source.body_consent_at IS NOT NULL
    JOIN authorization_chats access ON access.authorization_id = source.id
    JOIN chats chat ON chat.chat_id = access.chat_id
    WHERE target.id = %s AND role.updated_at = %s
      AND target.entity_type = 'person'
      AND access.is_visible AND access.sync_enabled
      AND COALESCE(chat.chat_status, 'normal') = 'normal'
      AND NOT EXISTS (
          SELECT 1 FROM lark_identities internal
          WHERE internal.contact_id = target.id AND internal.tenant_classification = 'internal'
      )
      AND (
          (COALESCE(chat.chat_mode, 'group') <> 'p2p' AND (
           EXISTS (
               SELECT 1 FROM chat_members member
               JOIN lark_identities identity ON identity.open_id = member.open_id
               JOIN authorizations witness ON witness.id = member.last_observed_by_authorization_id
               JOIN authorization_chats witnessed
                 ON witnessed.authorization_id = witness.id AND witnessed.chat_id = member.chat_id
               WHERE member.chat_id = chat.chat_id AND identity.contact_id = target.id
                 AND member.member_type = 'user' AND identity.identity_type = 'user'
                 AND identity.tenant_classification = 'known_external'
                 AND witness.status = 'active' AND witness.body_consent_at IS NOT NULL
                 AND witnessed.is_visible AND witnessed.sync_enabled
           ) OR EXISTS (
               SELECT 1 FROM chat_messages authored
               JOIN lark_identities identity ON identity.open_id = authored.sender_open_id
               JOIN authorization_message_observations seen
                 ON seen.chat_id = authored.chat_id AND seen.message_id = authored.message_id
                AND seen.authorization_id = source.id
               WHERE authored.chat_id = chat.chat_id AND identity.contact_id = target.id
                 AND authored.sender_type = 'user' AND NOT authored.is_deleted
                 AND identity.identity_type = 'user' AND identity.tenant_classification = 'known_external'
                 AND seen.observed_at >= source.body_consent_at
           )))
          OR (chat.chat_mode = 'p2p' AND access.p2p_counterpart_type = 'user'
              AND EXISTS (
                  SELECT 1 FROM lark_identities counterpart
                  WHERE counterpart.open_id = access.p2p_counterpart_open_id
                    AND counterpart.contact_id = target.id
                    AND counterpart.identity_type = 'user'
                    AND counterpart.tenant_classification = 'known_external'
              ))
      )
"""

_VISIBLE_MESSAGES_SQL = """
    WITH scoped_chat AS (
        SELECT chat.chat_id, source.id AS source_id, source.body_consent_at, target.id AS contact_id
""" + _CONVERSATION_SCOPE_SQL + """
          AND chat.chat_id = %s
    )
    SELECT message.body_ciphertext, message.message_create_time, message.message_id,
           message.message_type, message.sender_type, sender.display_name AS sender_name,
           sender.contact_id = (SELECT contact_id FROM scoped_chat LIMIT 1) AS sent_by_creator,
           EXISTS (
               SELECT 1 FROM contact_assignments assignment
               JOIN authorizations responsible ON responsible.id = assignment.responsible_authorization_id
               WHERE assignment.contact_id = (SELECT contact_id FROM scoped_chat LIMIT 1)
                 AND assignment.ended_at IS NULL AND responsible.open_id = message.sender_open_id
           ) AS sent_by_author
    FROM chat_messages message
    LEFT JOIN lark_identities sender ON sender.open_id = message.sender_open_id
    WHERE NOT message.is_deleted AND EXISTS (
        SELECT 1 FROM scoped_chat scope
        JOIN authorization_message_observations observation
          ON observation.authorization_id = scope.source_id AND observation.chat_id = scope.chat_id
        WHERE observation.chat_id = message.chat_id AND observation.message_id = message.message_id
          AND observation.observed_at >= scope.body_consent_at
    )
"""

_CONVERSATIONS_SQL = """
    WITH scoped_chats AS (
        SELECT chat.chat_id, chat.name, COALESCE(chat.chat_mode, 'group') AS chat_mode,
               source.id AS source_id, source.display_name AS source_name, source.body_consent_at
""" + _CONVERSATION_SCOPE_SQL + """
    ), conversation_list AS (
        SELECT chat_id, name, chat_mode, array_agg(DISTINCT source_name ORDER BY source_name) AS source_names
        FROM scoped_chats GROUP BY chat_id, name, chat_mode
    )
    SELECT conversation.*, counts.message_count, counts.last_message_at
    FROM conversation_list conversation
    JOIN LATERAL (
        SELECT COUNT(*) AS message_count, MAX(message.message_create_time) AS last_message_at
        FROM chat_messages message
        WHERE message.chat_id = conversation.chat_id AND NOT message.is_deleted AND EXISTS (
            SELECT 1 FROM scoped_chats scope
            JOIN authorization_message_observations observation
              ON observation.authorization_id = scope.source_id AND observation.chat_id = scope.chat_id
            WHERE scope.chat_id = message.chat_id AND observation.message_id = message.message_id
              AND observation.observed_at >= scope.body_consent_at
        )
    ) counts ON TRUE
    ORDER BY counts.last_message_at DESC NULLS LAST, conversation.name NULLS LAST, conversation.chat_id
"""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _retained_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= 500


def _label(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return re.sub(r"\b(?:ou|on|cli)_[A-Za-z0-9]+\b", "[标识已隐藏]", value)


def _handles(metadata: dict[str, Any]) -> list[str]:
    values = metadata.get("creator_handles", metadata.get("handles", []))
    if not isinstance(values, list):
        values = []
    values = [*values, metadata.get("creator_handle"), metadata.get("handle")]
    return sorted({value.lstrip("@") for value in values
                   if isinstance(value, str) and re.fullmatch(r"@?[\w.\-]{1,160}", value)})


def _rows(value: Any) -> list[int]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return sorted({int(str(row).strip()) for row in value
                   if re.fullmatch(r"[1-9]\d{0,6}", str(row).strip())})


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    audits = row.get("role_audits")
    audits = [_dict(value) for value in audits] if isinstance(audits, list) else []
    latest = audits[0] if audits else {}
    assignment = _dict(row.get("assignment_audit"))
    evidence = _dict(latest.get("evidence"))
    primary = _dict(evidence.get("primary_identity_evidence"))
    authority = _dict(evidence.get("manual_excel_authority"))
    rows = sorted({number for audit in audits for number in _rows(audit.get("excel_rows"))}
                  | set(_rows(authority.get("rows"))))
    handles = sorted({handle for audit in audits for handle in _handles(audit)} | set(_handles(assignment)))
    kind = primary.get("kind")
    group = primary.get("group_chat_id")
    valid_group = isinstance(group, str) and re.fullmatch(r"oc_[A-Za-z0-9]{8,80}", group)
    # The assignment audit must retain the same reviewed decision; a later manual
    # handover cannot silently inherit the previous owner's Excel attribution.
    documented = bool(
        row.get("role_source") == "manual_excel_and_identity_confirmed"
        and handles and rows and _retained_text(authority.get("owner_rule"))
        and _retained_text(latest.get("decision_id"))
        and latest.get("decision_id") == assignment.get("decision_id")
        and evidence == _dict(assignment.get("evidence"))
        and evidence.get("p2p_policy") == "auxiliary_only_not_used"
        and ((kind == "non_p2p_group_membership" and valid_group)
             or (kind == "excel_identity" and _retained_text(primary.get("resolution_rule"))))
    )
    if documented:
        reason = ("Excel 中的账号与负责人记录，结合已留存的目标群成员证据完成关联。"
                  if kind == "non_p2p_group_membership" else
                  "Excel 中的账号与负责人记录，结合表内姓名或明确别名完成联系人对应。")
    elif row.get("role_source") == "manual_p2p_confirmed":
        reason = "历史导入留存了单聊依据；本页未留存可独立核验的主证据，需要补充复核。"
    elif row.get("role_source") == "manual_excel_import":
        reason = "已留存 Excel 行号及人工匹配规则，但对应群的标识未留存，需要补充证据。"
    else:
        reason = "当前归属的主证据未完整留存，或归属已发生变化，需要补充复核。"
    return {
        "documented": documented, "handles": handles, "rows": rows, "reason": reason,
        "kind": kind, "group_ids": [group] if documented and valid_group else [],
        "owner_label": next((audit["owner_label"] for audit in audits
                             if isinstance(audit.get("owner_label"), str)), None),
    }


def _item(row: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "display_name": _label(row.get("display_name"), "姓名未返回"),
        "handles": facts["handles"], "responsible_id": row.get("responsible_id"),
        "responsible_name": _label(row.get("responsible_name"),
                                   "姓名未返回" if row.get("responsible_id") else "尚未分配"),
        "assignment_label": "已关联" if row.get("assignment_id") else "未分配",
        "source_label": {"manual_excel_import": "Excel 人工导入",
                         "manual_excel_and_identity_confirmed": "Excel 人工导入",
                         "manual_p2p_confirmed": "历史人工导入"}.get(row.get("role_source"), "历史记录"),
        "evidence_status": "documented" if facts["documented"] else "review",
        "evidence_label": "主证据已留存" if facts["documented"] else "主证据待补充",
        "evidence_reason": facts["reason"],
        "updated_at_label": _format_time(row.get("started_at") or row.get("reviewed_at")),
    }


def directory(page: int = 1, query: str = "", responsible_id: UUID | None = None,
              evidence: str = "all") -> dict[str, Any]:
    query = " ".join(query.split())[:120]
    evidence = evidence if evidence in {"all", "documented", "review"} else "all"
    with connection() as conn:
        rows = conn.execute(_CREATORS_SQL + " ORDER BY contact.display_name NULLS LAST, contact.id").fetchall()
    # ponytail: the reviewed registry is small; move evidence projection into SQL
    # if the registry grows to thousands, keeping this exact evidence policy.
    items = [_item(row, _evidence(row)) for row in rows]
    owners = {str(item["responsible_id"]): item["responsible_name"] for item in items if item["responsible_id"]}
    summary = {"creators": len(items), "handles": len({handle for item in items for handle in item["handles"]}),
               "documented": sum(item["evidence_status"] == "documented" for item in items)}
    summary["needs_review"] = summary["creators"] - summary["documented"]
    items = [item for item in items
             if (not query or query.casefold() in " ".join([item["display_name"], *item["handles"]]).casefold())
             and (not responsible_id or str(item["responsible_id"]) == str(responsible_id))
             and (evidence == "all" or item["evidence_status"] == evidence)]
    page, _, offset = _page_window(page, PAGE_SIZE, total_count=len(items))
    return {**_page_result(items[offset:offset + PAGE_SIZE], page, PAGE_SIZE, total_count=len(items)),
            "summary": summary, "owners": [{"id": key, "name": name} for key, name in sorted(
                owners.items(), key=lambda pair: pair[1])], "query": query, "evidence": evidence,
            "responsible_id": str(responsible_id) if responsible_id else ""}


def _scope_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row["id"], row["role_updated_at"])


def detail(contact_id: UUID) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(_CREATORS_SQL + " AND contact.id = %s", (contact_id,)).fetchone()
        if not row:
            return None
        facts = _evidence(row)
        conversations = conn.execute(_CONVERSATIONS_SQL, _scope_params(row)).fetchall()
    item = _item(row, facts)
    source_rows = "、".join(map(str, facts["rows"])) or "未留存"
    return {
        "creator": item, "source_label": item["source_label"], "source_rows_label": source_rows,
        "source_owner_label": (f"导入负责人标签：{_label(facts['owner_label'], '未留存')}；原 CM / TAP CM 字段未留存"
                               if facts["owner_label"] else "已留存负责人判断结论；原 CM / TAP CM 字段未留存"
                               if facts["documented"] else "原 CM / TAP CM 字段未留存"),
        "reviewed_at_label": _format_time(row.get("reviewed_at")),
        "evidence_steps": [
            {"title": "账号与负责人来源", "detail": f"{item['source_label']}；Excel 行号：{source_rows}。",
             "source": "导入审计"},
            {"title": "联系人对应依据", "detail": facts["reason"], "source": "已留存的人工复核记录"},
            {"title": "当前正式归属",
             "detail": f"{item['display_name']} → {item['responsible_name']}；{item['assignment_label']}。",
             "source": "当前正式列表"},
        ],
        "conversations": [{"chat_id": chat["chat_id"],
                           "name": _label(chat["name"], "相关单聊" if chat["chat_mode"] == "p2p" else "相关合作群"),
                           "chat_mode_label": "单聊" if chat["chat_mode"] == "p2p" else "群聊",
                           "context_label": "与达人单聊" if chat["chat_mode"] == "p2p" else "曾观察到达人账号参与",
                           "message_count": int(chat["message_count"]),
                           "last_message_at_label": _format_time(chat["last_message_at"]),
                           "source_label": "、".join(_label(name, "姓名未返回") for name in chat["source_names"])}
                          for chat in conversations],
        "conversation_notice": CONVERSATION_NOTICE,
    }


def _text_content(ciphertext: str | None, cipher: Any, message_type: str | None = "text") -> str:
    if ciphertext is None:
        return "（正文尚未同步）"
    try:
        # Reuse the existing plain-text renderer. It reads only the stored body;
        # no attachment fetches or follow-up reads of referenced conversations.
        text = _readable_body(cipher.decrypt(ciphertext), _message_type_label(message_type))
    except (InvalidTag, UnicodeDecodeError, ValueError, TypeError):
        return "（正文无法读取）"
    text = re.sub(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()\-]{6,}\d(?!\w)", "[电话号码已隐藏]", text)
    text = re.sub(r"\b(?:ou|on|cli)_[A-Za-z0-9]+\b", "[标识已隐藏]", text)
    return text


def messages(contact_id: UUID, chat_id: str, page: int = 1) -> dict[str, Any] | None:
    if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9]{8,80}", chat_id):
        return None
    with connection() as conn:
        row = conn.execute(_CREATORS_SQL + " AND contact.id = %s", (contact_id,)).fetchone()
        if not row:
            return None
        params = (*_scope_params(row), chat_id)
        if not conn.execute("SELECT chat.chat_id " + _CONVERSATION_SCOPE_SQL + " AND chat.chat_id = %s",
                            params).fetchone():
            return None
        total = conn.execute("SELECT COUNT(*) AS total_count FROM (" + _VISIBLE_MESSAGES_SQL + ") visible",
                             params).fetchone()["total_count"]
        page, limit, offset = _page_window(page, MESSAGE_PAGE_SIZE, total_count=total)
        rows = conn.execute(_VISIBLE_MESSAGES_SQL + """
            ORDER BY message_create_time DESC NULLS LAST, message_id DESC LIMIT %s OFFSET %s
        """, (*params, limit, offset)).fetchall()
    cipher = message_body_cipher(get_settings().token_encryption_key)
    items = [{"sender_label": _label(message["sender_name"] or (
                  row["display_name"] if message["sent_by_creator"] else row["responsible_name"]
                  if message["sent_by_author"] else None), "姓名未返回"),
              "sender_role_label": "达人" if message["sent_by_creator"] else "当前负责人"
              if message["sent_by_author"] else "其他参与者" if message["sender_type"] == "user" else "应用 / 系统",
              "created_at_label": _message_time(message["message_create_time"]),
              "content": _text_content(message["body_ciphertext"], cipher, message["message_type"]),
              "message_type_label": _message_type_label(message["message_type"]),
              "sent_by_author": message["sent_by_author"]} for message in rows[:MESSAGE_PAGE_SIZE]]
    return {**_page_result(items, page, MESSAGE_PAGE_SIZE, total_count=total), "notice": CONVERSATION_NOTICE}


def record_view(viewer_id: UUID, contact_id: UUID, chat_id: str) -> None:
    with connection() as conn, conn.transaction():
        conn.execute("""
            INSERT INTO audit_events (id, authorization_id, action, metadata)
            VALUES (%s, %s, 'creator_review_messages_viewed', %s::jsonb)
        """, (uuid4(), viewer_id, json.dumps({"contact_id": str(contact_id), "chat_id": chat_id})))

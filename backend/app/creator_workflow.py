"""Creator/CM intake with optional social accounts; candidates never grant chat access."""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .db import connection

# Explicit frontend pilot grants, independent of retired admin review pages.
TEST_CM_ID = UUID("24d1ef13-3762-4359-ba11-d26c03cf1f7f")
TEST_EDITOR_IDS = frozenset({
    UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0"),
    UUID("1225b4a0-fbd8-431e-962e-25e66943a8a0"),
})
CAP_POLICY_VERSION = "cap_v1"
BUSINESS_POLICY_VERSION = "business_v2"
AUTO_CONFIRM_THRESHOLD = 0.95
_UNSET = object()


def normalize_handle(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lstrip("@").casefold()
    return value if re.fullmatch(r"[\w.\-]{1,160}", value) else ""


class WorkflowInputError(ValueError):
    """Incomplete, conflicting, or stale intake data; no partial promotion is allowed."""


REASON_LABELS = {
    "missing_creator_type": "请选择 CAP 或非 CAP",
    "creator_type_conflict": "达人类型与现有记录冲突",
    "missing_handle": "请补充达人 handle",
    "missing_cm": "负责人尚未确定",
    "missing_user": "请选择对应 Lark 用户",
    "contact_unavailable": "所选用户不在当前可选范围",
    "ownership_conflict": "与现有负责人冲突，请管理员核对",
    "identity_conflict": "与现有账号身份关联冲突",
    "identity_risk": "历史身份关系需要人工核对",
    "ai_review": "请核对 AI 推荐的账号、负责人和用户",
    "identity_uncertain": "对应用户尚不确定",
    "ownership_uncertain": "负责人尚不确定",
    "handle_uncertain": "handle 尚不确定",
    "source_conflict": "新来源与已记录信息不一致",
    "missing_relationship": "请选择达人本人或代表关系",
    "details_changed": "资料已修改，请重新核对账号、用户及关系",
}
COMPUTED_REASONS = frozenset(
    {
        "missing_handle",
        "missing_cm",
        "missing_user",
        "contact_unavailable",
        "ownership_conflict",
        "identity_conflict",
        "missing_relationship",
        "missing_creator_type",
        "creator_type_conflict",
    }
)


def _uuid(value: Any, *, optional: bool = False) -> UUID | None:
    if optional and (value is None or value == ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowInputError("用户、记录或请求 ID 无效。") from exc


def _handle(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise WorkflowInputError("请输入文字格式的 handle。")
    value = value.strip()
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise WorkflowInputError("主页链接格式不正确，请填写 handle 或有效 TikTok 主页链接。") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.netloc.lower() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
            or not parsed.path.startswith("/@")
            or "/" in parsed.path[2:].rstrip("/")
        ):
            raise WorkflowInputError("请填写 handle 或 TikTok 主页链接，不支持视频、短链或其他网站链接。")
        value = parsed.path[2:].rstrip("/")
    result = normalize_handle(value)
    if not result or value in {"—", "-"}:
        raise WorkflowInputError("请输入有效 handle；空字段请留空，不要填写横线或用户姓名。")
    return result


def _creator_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value not in {"cap", "non_cap"}:
        raise WorkflowInputError("达人类型只能选择 CAP、非 CAP，或留空待确认。")
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _audit(conn: Any, actor_id: UUID, action: str, data: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events(id, authorization_id, action, metadata) VALUES(%s,%s,%s,%s::jsonb)",
        (uuid4(), actor_id, "creator_workflow_" + action, json.dumps(data, default=str, ensure_ascii=False)),
    )


def _scope(conn: Any, actor_id: UUID, responsible_id: UUID | None, *, lock: bool = False) -> dict:
    if lock:
        # ponytail: serialize the small intake write queue, including two unknown
        # records merging into one account; use ordered per-account locks at scale.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('creator-workflow-write',0))")
    actor_id = _uuid(actor_id)
    target_id = _uuid(responsible_id, optional=True)
    ids = sorted({actor_id, *([target_id] if target_id else [])}, key=str)
    suffix = " FOR SHARE" if lock else ""
    users = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, display_name, status, assignment_eligible FROM authorizations "
            "WHERE id=ANY(%s::uuid[]) ORDER BY id" + suffix,
            (ids,),
        ).fetchall()
    }
    actor = users.get(actor_id)
    if not actor or actor["status"] != "active":
        raise PermissionError("当前登录授权已失效。")
    role = conn.execute(
        "SELECT role, is_active FROM admin_roles WHERE authorization_id=%s" + suffix, (actor_id,)
    ).fetchone()
    owner = bool(role and role["role"] == "owner" and role["is_active"])
    if target_id is None and not owner:
        target_id = actor_id
    target = users.get(target_id)
    if target_id and not target:
        raise LookupError("找不到该负责人。")
    own_list = bool(target_id == actor_id and actor["assignment_eligible"])
    test_editor = bool(
        actor_id in TEST_EDITOR_IDS and target_id == TEST_CM_ID
        and target and target["status"] == "active" and target["assignment_eligible"]
        and (owner or actor_id != UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0"))
        and not (role and role["role"] == "owner" and not owner)
    )
    if target_id != actor_id and not owner and not test_editor:
        raise PermissionError("只能查看自己的达人名单。")
    can_write = own_list or test_editor
    return {
        "actor_id": str(actor_id),
        "responsible_id": str(target_id) if target_id else None,
        "responsible_name": target["display_name"] if target else "管理员待确认列表",
        "can_write": can_write,
        "test_editor": test_editor,
        "can_import": own_list or owner,
        "can_scan": own_list or owner,
        "can_admin": owner,
        "is_owner": owner,
        "readonly": not can_write,
    }


def get_scope(actor_id: UUID, responsible_id: UUID | None = None) -> dict:
    with connection() as conn:
        return _scope(conn, actor_id, responsible_id)


def list_responsibles(actor_id: UUID) -> list[dict]:
    with connection() as conn:
        scope = _scope(conn, actor_id, None)
        if not scope["is_owner"]:
            return [{"id": scope["responsible_id"], "display_name": scope["responsible_name"]}]
        rows = conn.execute(
            "SELECT id,display_name FROM authorizations WHERE status='active' AND assignment_eligible "
            "ORDER BY display_name,id"
        ).fetchall()
        return [{"id": str(row["id"]), "display_name": row["display_name"]} for row in rows]


def _fresh(conn: Any, scope: dict, *, write: bool = False) -> dict:
    current = _scope(conn, _uuid(scope["actor_id"]), _uuid(scope.get("responsible_id"), optional=True), lock=write)
    if write and not current["can_write"]:
        raise PermissionError("只有负责人本人或已授权的测试账号可以补资料和确认。")
    return current


_SELECTABLE_SQL = """
    WITH scoped_authorization AS MATERIALIZED (
        SELECT id,open_id,body_consent_at FROM authorizations
        WHERE id=%s AND status='active' AND body_consent_at IS NOT NULL
    ), visible_chats AS MATERIALIZED (
        SELECT access.chat_id,access.p2p_counterpart_open_id,access.p2p_counterpart_type,
               auth.id AS authorization_id,auth.body_consent_at
        FROM scoped_authorization auth JOIN authorization_chats access ON access.authorization_id=auth.id
        WHERE access.is_visible AND access.sync_enabled
    ), identities AS MATERIALIZED (
        SELECT identity.contact_id,identity.open_id,
               COALESCE(identity.display_name,contact.display_name,identity.open_id) AS display_name
        FROM scoped_authorization auth JOIN lark_identities identity
          ON identity.identity_type='user' AND identity.contact_id IS NOT NULL AND identity.open_id<>auth.open_id
        JOIN contacts contact ON contact.id=identity.contact_id
        WHERE (%s::uuid[] IS NULL OR identity.contact_id=ANY(%s::uuid[]))
          AND (%s='' OR identity.display_name ILIKE %s ESCAPE '\\' OR contact.display_name ILIKE %s ESCAPE '\\'
            OR identity.open_id=%s)
    ), visible_users AS (
        SELECT identity.* FROM identities identity JOIN visible_chats access
          ON access.p2p_counterpart_type='user' AND access.p2p_counterpart_open_id=identity.open_id
        UNION
        SELECT identity.* FROM identities identity JOIN chat_members member ON member.open_id=identity.open_id
        JOIN visible_chats access ON access.chat_id=member.chat_id
          AND member.member_type='user' AND member.last_observed_by_authorization_id=access.authorization_id
        UNION
        SELECT identity.* FROM identities identity JOIN chat_messages message ON message.sender_open_id=identity.open_id
        JOIN visible_chats access ON access.chat_id=message.chat_id
        JOIN authorization_message_observations observation ON observation.authorization_id=access.authorization_id
          AND observation.chat_id=message.chat_id AND observation.message_id=message.message_id
        WHERE NOT message.is_deleted AND observation.observed_at>=access.body_consent_at
    )
    SELECT * FROM visible_users ORDER BY display_name,open_id LIMIT %s
"""


def selectable_users(
    conn: Any, responsible_id: UUID, query: str = "", contact_id: UUID | None = None, limit: int = 20
) -> list[dict]:
    """No global directory query, no sync side effect, and no candidate-derived access."""
    return _selectable_users(conn, responsible_id, query, [contact_id] if contact_id else None, min(max(limit, 1), 200))


def _selectable_users(
    conn: Any, responsible_id: UUID, query: str, contact_ids: list[UUID] | None, limit: int | None
) -> list[dict]:
    if not responsible_id:
        return []
    query = str(query).strip()[:100]
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        _SELECTABLE_SQL,
        (_uuid(responsible_id), contact_ids, contact_ids, query, pattern, pattern, query, limit),
    ).fetchall()
    return [
        {
            "contact_id": str(r["contact_id"]),
            "open_id": r["open_id"],
            "display_name": r["display_name"],
            "avatar_url": None,
            "identity_hint": r["open_id"],
        }
        for r in rows
    ]


def search_users(scope: dict, query: str, limit: int = 20) -> list[dict]:
    with connection() as conn, conn.transaction():
        scope = _fresh(conn, scope, write=True)
        if not query.strip():
            return []
        return selectable_users(conn, _uuid(scope["responsible_id"]), query, limit=limit)


def validate_evidence(conn: Any, responsible_id: UUID | None, refs: list[dict]) -> list[dict]:
    if not isinstance(refs, list) or not 1 <= len(refs) <= 80:
        raise WorkflowInputError("AI 推荐必须带有当前有效授权下的消息依据。")
    checked = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise WorkflowInputError("消息依据格式不正确。")
        auth_id = _uuid(ref.get("authorization_id") or responsible_id)
        if responsible_id and auth_id != _uuid(responsible_id):
            raise PermissionError("消息依据不属于候选负责人的授权范围。")
        row = conn.execute(
            """
            SELECT observation.chat_id, observation.message_id
            FROM authorizations auth
            JOIN authorization_chats access ON access.authorization_id=auth.id
            JOIN chats conversation ON conversation.chat_id=access.chat_id
            JOIN authorization_message_observations observation
              ON observation.authorization_id=auth.id AND observation.chat_id=access.chat_id
            JOIN chat_messages message ON message.chat_id=observation.chat_id
              AND message.message_id=observation.message_id
            WHERE auth.id=%s AND auth.status='active' AND auth.body_consent_at IS NOT NULL
              AND access.is_visible AND access.sync_enabled AND observation.observed_at >= auth.body_consent_at
              AND COALESCE(conversation.chat_status,'normal')='normal'
              AND observation.chat_id=%s AND observation.message_id=%s AND NOT message.is_deleted
              AND message.body_ciphertext IS NOT NULL
            FOR SHARE OF auth, access, observation, message
        """,
            (auth_id, ref.get("chat_id"), ref.get("message_id")),
        ).fetchone()
        if not row:
            raise PermissionError("消息依据已不可见、未授权或已删除，请重新核对。")
        checked.append({"authorization_id": str(auth_id), "chat_id": row["chat_id"], "message_id": row["message_id"]})
    return checked


def _lock_contact_scope(conn: Any, responsible_id: UUID, contact_id: UUID) -> dict:
    # Block concurrent OAuth/chat revocation until the selection transaction commits.
    conn.execute("SELECT id FROM authorizations WHERE id=%s FOR SHARE", (responsible_id,)).fetchall()
    conn.execute(
        "SELECT chat_id FROM authorization_chats WHERE authorization_id=%s ORDER BY chat_id FOR SHARE",
        (responsible_id,),
    ).fetchall()
    conn.execute("SELECT id FROM contacts WHERE id=%s FOR SHARE", (contact_id,)).fetchall()
    conn.execute(
        "SELECT open_id FROM lark_identities WHERE contact_id=%s ORDER BY open_id FOR SHARE", (contact_id,)
    ).fetchall()
    # Keep at least one qualifying membership/message fact stable until commit.
    # Locking only authorization_chats would miss concurrent removal of the last
    # selectable-user observation while leaving the chat itself authorized.
    conn.execute(
        """SELECT member.chat_id,member.open_id FROM chat_members member
        JOIN lark_identities identity ON identity.open_id=member.open_id
        JOIN authorization_chats access ON access.chat_id=member.chat_id
        WHERE access.authorization_id=%s AND access.is_visible AND access.sync_enabled
          AND identity.contact_id=%s AND member.member_type='user'
          AND member.last_observed_by_authorization_id=%s
        ORDER BY member.chat_id,member.open_id LIMIT 1 FOR SHARE OF member""",
        (responsible_id, contact_id, responsible_id),
    ).fetchall()
    conn.execute(
        """SELECT observation.chat_id,observation.message_id FROM authorization_message_observations observation
        JOIN authorizations auth ON auth.id=observation.authorization_id
        JOIN authorization_chats access
          ON access.authorization_id=auth.id AND access.chat_id=observation.chat_id
        JOIN chat_messages message ON message.chat_id=observation.chat_id
          AND message.message_id=observation.message_id
        JOIN lark_identities identity ON identity.open_id=message.sender_open_id
        WHERE auth.id=%s AND access.is_visible AND access.sync_enabled AND identity.contact_id=%s
          AND observation.observed_at >= auth.body_consent_at AND NOT message.is_deleted
        ORDER BY observation.chat_id,observation.message_id LIMIT 1 FOR SHARE OF observation,message""",
        (responsible_id, contact_id),
    ).fetchall()
    options = selectable_users(conn, responsible_id, contact_id=contact_id)
    if not options:
        raise PermissionError("所选 Lark 用户不存在或已不在你的可选范围，请重新搜索。")
    return options[0]


def _conflicts(conn: Any, row: dict, *, manual: bool = False) -> list[str]:
    people = conn.execute(
        "SELECT responsible_authorization_id,creator_type FROM creator_account_assignments "
        "WHERE contact_id=%s AND ended_at IS NULL", (row.get("contact_id"),),
    ).fetchall() if row.get("contact_id") else []
    reasons = []
    for known in people:
        if str(known["responsible_authorization_id"]) != str(row.get("responsible_authorization_id")):
            reasons.append("ownership_conflict")
        if known.get("creator_type") and row.get("creator_type") and known["creator_type"] != row["creator_type"]:
            reasons.append("creator_type_conflict")
    if not row.get("normalized_handle"):
        return sorted(set(reasons))
    owners = conn.execute(
        """
        SELECT assignment.responsible_authorization_id, assignment.contact_id, assignment.relationship,
               assignment.creator_type
        FROM social_accounts account JOIN creator_account_assignments assignment
          ON assignment.social_account_id=account.id AND assignment.ended_at IS NULL
        WHERE account.platform=%s AND account.normalized_handle=%s
    """,
        (row["platform"], row["normalized_handle"]),
    ).fetchall()
    # A legacy person may have other accounts. Only the exact account's old mapping matters.
    legacy = conn.execute(
        """
        SELECT mapping.contact_id, mapping.relationship, assignment.responsible_authorization_id
        FROM social_accounts account JOIN contact_social_accounts mapping
          ON mapping.social_account_id=account.id AND mapping.status='confirmed'
        LEFT JOIN contact_assignments assignment ON assignment.contact_id=mapping.contact_id
          AND assignment.ended_at IS NULL
        WHERE account.platform=%s AND account.normalized_handle=%s
    """,
        (row["platform"], row["normalized_handle"]),
    ).fetchall()
    for known in owners:
        if known.get("creator_type") and row.get("creator_type") and known["creator_type"] != row["creator_type"]:
            reasons.append("creator_type_conflict")
        if (row.get("contact_id") and str(known["contact_id"]) != str(row["contact_id"])) or (
            not manual and known["relationship"] != row["relationship"]
        ):
            reasons.append("identity_conflict")
    for known in owners + legacy:
        if known["responsible_authorization_id"] and str(known["responsible_authorization_id"]) != str(
            row.get("responsible_authorization_id")
        ):
            reasons.append("ownership_conflict")
        if (
            row.get("contact_id")
            and (manual or known["relationship"] == row["relationship"])
            and str(known["contact_id"]) != str(row["contact_id"])
        ):
            reasons.append("identity_conflict")
    return sorted(set(reasons))


def _reasons(conn: Any, row: dict, contact: dict | None) -> list[dict]:
    codes = list(row.get("uncertainties") or [])
    for field, reason in (
        ("normalized_handle", "missing_handle"),
        ("responsible_authorization_id", "missing_cm"),
        ("contact_id", "missing_user"),
        ("relationship", "missing_relationship"),
        ("creator_type", "missing_creator_type"),
    ):
        if not row.get(field):
            codes.append(reason)
    if row.get("contact_id") and not contact:
        codes.append("contact_unavailable")
    codes += _conflicts(conn, row, manual=True)
    return [
        {"code": code, "label": REASON_LABELS.get(code, code), "resolvable": code not in COMPUTED_REASONS}
        for code in dict.fromkeys(codes)
    ]


def _record(
    conn: Any, row: dict, scope: dict, *, contacts: dict | None = None, source_rows: dict | None = None
) -> dict:
    # An importer may discover that its handle already exists elsewhere. A safe
    # receipt is not a read grant to that other CM's source material or identity.
    outside_scope = not scope["is_owner"] and str(row["responsible_authorization_id"]) != scope["responsible_id"]
    if contacts is not None:
        contact = contacts.get((row["responsible_authorization_id"], str(row.get("contact_id"))))
    else:
        options = (
            selectable_users(conn, row["responsible_authorization_id"], contact_id=row.get("contact_id"))
            if row.get("contact_id") and not outside_scope
            else []
        )
        contact = options[0] if options else None
    if outside_scope:
        contact = None
    sources = (
        [dict(item) for item in source_rows.get(row["id"], [])]
        if source_rows is not None
        else conn.execute(
            "SELECT id, source_type, source_key, source_data, created_at FROM creator_workflow_sources "
            "WHERE candidate_id=%s ORDER BY created_at,id",
            (row["id"],),
        ).fetchall()
    )
    if outside_scope:
        sources = []
    else:
        for item in sources:
            if item["source_type"] == "ai":
                try:
                    validate_evidence(conn, row["responsible_authorization_id"], item["source_data"].get("evidence"))
                except (WorkflowInputError, PermissionError):
                    item["source_key"] = ""
                    item["source_data"] = {
                        "unavailable": True,
                        "notice": "消息依据当前不可查看，请按当前有权查看的资料核对。",
                    }
    reasons = (
        [{"code": "ownership_conflict", "label": "已交管理员核对，不会覆盖现有归属", "resolvable": False}]
        if outside_scope
        else _reasons(conn, row, contact)
        if row["status"] != "confirmed"
        else []
    )
    source_types = sorted({s["source_type"] for s in sources})
    blocking_reasons = [r for r in reasons if r["code"] in COMPUTED_REASONS - {
        "missing_relationship", "missing_handle", "missing_creator_type",
    }]
    return {
        "id": str(row["id"]),
        "version": row["version"],
        "platform": row["platform"],
        "handle": row["normalized_handle"],
        "contact": contact,
        "contact_id": contact["contact_id"] if contact else None,
        "relationship": row["relationship"],
        "creator_type": row.get("creator_type"),
        "responsible_id": str(row["responsible_authorization_id"])
        if row["responsible_authorization_id"] and not outside_scope
        else None,
        "responsible_name": row.get("responsible_name"),
        "status": row["status"],
        "status_label": {
            "pending": "待确认",
            "deferred": "待确认 · 暂缓",
            "rejected": "待管理员核对",
            "confirmed": "已确认入库",
            "merged": "已合并",
        }[row["status"]],
        "sources": source_types,
        "source_label": " + ".join("Excel" if s == "excel" else "AI" for s in source_types),
        "reasons": reasons,
        "blocking_reasons": blocking_reasons,
        "evidence": [{**s, "id": str(s["id"])} for s in sources],
        "can_confirm": bool(
            scope["can_write"] and row["status"] in {"pending", "deferred"} and not blocking_reasons
        ),
    }


def _candidate(conn: Any, candidate_id: UUID, scope: dict, *, lock: bool = False) -> dict:
    row = conn.execute(
        "SELECT * FROM creator_workflow_candidates WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (_uuid(candidate_id),),
    ).fetchone()
    if not row or row["status"] == "merged":
        raise LookupError("这条记录不存在或已合并，请刷新列表。")
    if scope["responsible_id"] is not None and str(row["responsible_authorization_id"]) != scope["responsible_id"]:
        raise PermissionError("这条记录不在当前负责人的名单中。")
    if scope["responsible_id"] is None and not scope["is_owner"]:
        raise PermissionError("不能访问管理员待确认列表。")
    return row


def get_record(scope: dict, candidate_id: UUID) -> dict:
    with connection() as conn:
        scope = _fresh(conn, scope)
        return _record(conn, _candidate(conn, candidate_id, scope), scope)


def list_records(
    scope: dict,
    status: str = "all",
    source: str = "",
    reason: str = "",
    query: str = "",
    page: int = 1,
    page_size: int = 50,
    type_filter: str = "all",
) -> dict:
    with connection() as conn:
        scope = _fresh(conn, scope)
        rows = conn.execute(
            """
            SELECT candidate.*, responsible.display_name AS responsible_name
            FROM creator_workflow_candidates candidate
            LEFT JOIN authorizations responsible ON responsible.id=candidate.responsible_authorization_id
            WHERE candidate.merged_into IS NULL
              AND (candidate.normalized_handle IS NOT NULL OR candidate.contact_id IS NOT NULL)
              AND (candidate.status <> 'confirmed' OR EXISTS (SELECT 1 FROM creator_account_assignments formal
                WHERE formal.candidate_id=candidate.id AND formal.ended_at IS NULL))
              AND ((%s::uuid IS NULL AND candidate.responsible_authorization_id IS NULL)
                OR candidate.responsible_authorization_id=%s)
              AND (%s OR candidate.status <> 'rejected')
            ORDER BY (candidate.status='confirmed'), candidate.updated_at DESC, candidate.id
        """,
            (scope["responsible_id"], scope["responsible_id"], scope["is_owner"]),
        ).fetchall()
        # Read a CM's permitted identities once, not once per row. This cache lives
        # only inside this request; every draft/confirmation still rechecks live scope.
        ids_by_cm: dict[UUID, set[UUID]] = {}
        for row in rows:
            if row["responsible_authorization_id"] and row["contact_id"]:
                ids_by_cm.setdefault(row["responsible_authorization_id"], set()).add(row["contact_id"])
        contacts = {}
        for cm_id, contact_ids in ids_by_cm.items():
            for contact in _selectable_users(conn, cm_id, "", sorted(contact_ids, key=str), None):
                contacts.setdefault((cm_id, contact["contact_id"]), contact)
        source_rows: dict[UUID, list[dict]] = {}
        if rows:
            for item in conn.execute(
                "SELECT id,candidate_id,source_type,source_key,source_data,created_at FROM creator_workflow_sources "
                "WHERE candidate_id=ANY(%s::uuid[]) ORDER BY created_at,id",
                ([row["id"] for row in rows],),
            ).fetchall():
                candidate_id = item.pop("candidate_id")
                source_rows.setdefault(candidate_id, []).append(item)
        # ponytail: keep reason filtering in Python for small rosters; move it
        # into SQL if a single CM's roster grows past thousands of accounts.
        items = [_record(conn, row, scope, contacts=contacts, source_rows=source_rows) for row in rows]
    summary = {
        "total": len(items),
        "pending": sum(r["status"] != "confirmed" for r in items),
        "confirmed": sum(r["status"] == "confirmed" for r in items),
        "deferred": sum(r["status"] == "deferred" for r in items),
    }
    if status not in {"all", "pending", "confirmed", "deferred", "rejected"}:
        raise WorkflowInputError("无效的状态筛选。")
    if source and source not in {"excel", "ai"}:
        raise WorkflowInputError("无效的来源筛选。")
    if type_filter not in {"all", "cap", "non_cap", "unknown"}:
        raise WorkflowInputError("无效的达人类型筛选。")
    query = query.strip().lstrip("@").casefold()[:100]
    items = [
        r
        for r in items
        if (status == "all" or r["status"] == status or status == "pending" and r["status"] in {"deferred", "rejected"})
        and (not source or source in r["sources"])
        and (type_filter == "all" or (r["creator_type"] or "unknown") == type_filter)
        and (not reason or any(x["code"] == reason for x in r["reasons"]))
        and (
            not query
            or query in (r["handle"] or "").casefold()
            or query in ((r["contact"] or {}).get("display_name") or "").casefold()
        )
    ]
    page_size = max(1, min(int(page_size), 100))
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page), pages))
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "summary": summary,
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "scope": scope,
    }


def _version(row: dict, version: int) -> None:
    if type(version) is not int or row["version"] != version:
        raise WorkflowInputError("这条记录刚刚发生了变化，请刷新后再操作。")


def _request(conn: Any, scope: dict, request_id: UUID | None, payload: dict) -> tuple[UUID, str, dict | None]:
    request_id = _uuid(request_id) if request_id else uuid4()
    digest = _digest(payload)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"creator-request:{scope['actor_id']}:{request_id}",)
    )
    replay = conn.execute(
        "SELECT fingerprint,candidate_id FROM creator_workflow_requests WHERE actor_id=%s AND request_id=%s",
        (scope["actor_id"], request_id),
    ).fetchone()
    if replay and replay["fingerprint"] != digest:
        raise WorkflowInputError("这个请求编号已经用于另一项操作，请刷新后重试。")
    return request_id, digest, replay


def _finish(conn: Any, scope: dict, row: dict, request_id: UUID, digest: str, action: str, before: dict) -> dict:
    conn.execute(
        "INSERT INTO creator_workflow_requests(actor_id,request_id,fingerprint,candidate_id) VALUES(%s,%s,%s,%s)",
        (scope["actor_id"], request_id, digest, row["id"]),
    )
    _audit(
        conn,
        _uuid(scope["actor_id"]),
        action,
        {"candidate_id": row["id"], "before": before, "after": row, "request_id": request_id},
    )
    return _record(conn, row, scope)


def _editable(row: dict) -> None:
    if row["status"] not in {"pending", "deferred"}:
        raise WorkflowInputError("只有待确认记录可以修改；正式关联和已否认记录须另行核对。")


def recheck_candidate(scope: dict, candidate_id: UUID, version: int) -> dict:
    """Suggest current-scope users for saved data, without selecting or promoting anyone."""
    with connection() as conn, conn.transaction():
        scope = _fresh(conn, scope, write=True)
        row = _candidate(conn, candidate_id, scope)
        _version(row, version)
        _editable(row)
        record = _record(conn, row, scope)
        handle = row["normalized_handle"]
        suggestions = {}
        if handle:
            known = conn.execute(
                """SELECT DISTINCT mapping.contact_id FROM social_accounts account
                JOIN contact_social_accounts mapping ON mapping.social_account_id=account.id
                WHERE account.platform=%s AND account.normalized_handle=%s AND mapping.status='confirmed'""",
                (row["platform"], handle),
            ).fetchall()
            exact = (
                _selectable_users(conn, row["responsible_authorization_id"], "", [m["contact_id"] for m in known], 20)
                if known else []
            )
            for user in exact:
                suggestions[user["contact_id"]] = {
                    **user, "match_type": "existing_account", "match_label": "已有账号关联，仍需你核对",
                }
            for user in selectable_users(conn, row["responsible_authorization_id"], query=handle):
                suggestions.setdefault(user["contact_id"], {
                    **user, "match_type": "name_match", "match_label": "名称包含 handle，仅供参考",
                })
        items = list(suggestions.values())[:20]
        notice = (
            "请先补充 handle 并保存；也可以直接搜索你拥有的用户。" if not handle else
            "已重新检查。以下是你当前可见范围内的建议，需你选择并核对，不会自动关联。" if items else
            "已重新检查，暂未找到建议用户。可手动搜索你拥有的用户；未补齐的信息仍保留待确认。"
        )
        if any(r["code"] in {"ownership_conflict", "identity_conflict"} for r in record["reasons"]):
            notice = "已重新检查，账号与现有归属或用户关联存在冲突，不能直接入库，请核对。"
        return {
            "candidate_id": record["id"], "version": record["version"], "handle": handle,
            "items": items, "reasons": record["reasons"], "notice": notice,
        }


def save_draft(
    scope: dict,
    candidate_id: UUID,
    version: int,
    *,
    handle: str | None,
    contact_id: UUID | None,
    relationship: str | None = None,
    creator_type: Any = _UNSET,
    resolved_reasons: list[str] | tuple = (),
    request_id: UUID | None = None,
) -> dict:
    handle, contact_id = _handle(handle), _uuid(contact_id, optional=True)
    if creator_type is not _UNSET:
        creator_type = _creator_type(creator_type)
    if relationship not in {None, "owner", "representative"}:
        raise WorkflowInputError("请选择本人或代表关系。")
    if not isinstance(resolved_reasons, list | tuple) or any(not isinstance(r, str) for r in resolved_reasons):
        raise WorkflowInputError("核对项格式不正确。")
    payload = {
        "action": "draft",
        "candidate_id": candidate_id,
        "version": version,
        "handle": handle,
        "contact_id": contact_id,
        "relationship": relationship,
        "creator_type": "unchanged" if creator_type is _UNSET else creator_type,
        "resolved": sorted(set(resolved_reasons)),
    }
    with connection() as conn, conn.transaction():
        scope = _fresh(conn, scope, write=True)
        request_id, digest, replay = _request(conn, scope, request_id, payload)
        if replay:
            return _record(conn, _candidate(conn, replay["candidate_id"], scope), scope)
        if handle:
            original = _candidate(conn, candidate_id, scope)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"creator-account:{original['platform']}:{handle}",),
            )
        row = _candidate(conn, candidate_id, scope, lock=True)
        _version(row, version)
        if creator_type is _UNSET:
            creator_type = row.get("creator_type")
        if row["status"] == "confirmed":
            if (
                contact_id != row["contact_id"]
                or relationship not in {None, row["relationship"]}
                or row["normalized_handle"] and handle != row["normalized_handle"]
                or row.get("creator_type") and creator_type != row["creator_type"]
                or resolved_reasons
            ):
                raise WorkflowInputError("已确认达人只能补充首次 handle 或未知类型，不能更换用户、负责人或已有资料。")
            if not conn.execute(
                "SELECT id FROM creator_account_assignments WHERE candidate_id=%s AND ended_at IS NULL FOR UPDATE",
                (row["id"],),
            ).fetchone():
                raise WorkflowInputError("这位达人的正式负责关系已结束，请重新核对。")
            updated = _promote(conn, {**row, "normalized_handle": handle, "creator_type": creator_type},
                               scope, "cm_confirmation")
            return _finish(conn, scope, updated, request_id, digest, "confirmed_details_added", row)
        _editable(row)
        if set(resolved_reasons) - set(row["uncertainties"]):
            raise WorkflowInputError("只能解除当前记录中明确列出的待核对项。")
        # Keep source metadata for the same pair, never carry an old role to edits.
        if relationship is None and (handle, contact_id) == (row["normalized_handle"], row["contact_id"]):
            relationship = row["relationship"]
        changed = (handle, contact_id, relationship, creator_type) != (
            row["normalized_handle"], row["contact_id"], row["relationship"], row.get("creator_type")
        )
        # Keep an identity-edit trace for audit; manual confirmation resolves it.
        uncertainties = set(row["uncertainties"]) - set(resolved_reasons)
        if changed:
            uncertainties.add("details_changed")
        if contact_id:
            _lock_contact_scope(conn, _uuid(scope["responsible_id"]), contact_id)
        if handle:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"creator-account:{row['platform']}:{handle}",)
            )
            duplicate = conn.execute(
                """SELECT * FROM creator_workflow_candidates
                WHERE platform=%s AND normalized_handle=%s AND merged_into IS NULL
                  AND status <> 'confirmed' AND id<>%s FOR UPDATE""",
                (row["platform"], handle, row["id"]),
            ).fetchone()
            if duplicate:
                if (
                    str(duplicate["responsible_authorization_id"]) != scope["responsible_id"]
                    or duplicate["status"] not in {"pending", "deferred"}
                    or contact_id
                    and duplicate["contact_id"]
                    and duplicate["contact_id"] != contact_id
                    or relationship and duplicate["relationship"] and duplicate["relationship"] != relationship
                    or creator_type and duplicate.get("creator_type") and duplicate["creator_type"] != creator_type
                ):
                    raise WorkflowInputError("这个 handle 已在另一条记录中，请先核对现有记录，不能覆盖或重复入库。")
                conn.execute(
                    "UPDATE creator_workflow_sources SET candidate_id=%s WHERE candidate_id=%s",
                    (duplicate["id"], row["id"]),
                )
                conn.execute(
                    "UPDATE creator_workflow_candidates SET status='merged',merged_into=%s,"
                    "version=version+1,updated_at=NOW() WHERE id=%s",
                    (duplicate["id"], row["id"]),
                )
                updated = conn.execute(
                    """UPDATE creator_workflow_candidates SET contact_id=COALESCE(%s,contact_id),
                    relationship=%s,creator_type=%s,
                    uncertainties=%s,version=version+1,updated_at=NOW() WHERE id=%s RETURNING *""",
                    (
                        contact_id,
                        duplicate["relationship"] if not contact_id or contact_id == duplicate["contact_id"]
                        else relationship,
                        creator_type or duplicate.get("creator_type"),
                        sorted(uncertainties | (set(duplicate["uncertainties"]) - set(resolved_reasons))),
                        duplicate["id"],
                    ),
                ).fetchone()
                return _finish(conn, scope, updated, request_id, digest, "candidate_merged", row)
        updated = conn.execute(
            """UPDATE creator_workflow_candidates SET normalized_handle=%s,contact_id=%s,
            relationship=%s,creator_type=%s,
            uncertainties=%s,status='pending',version=version+1,updated_at=NOW() WHERE id=%s RETURNING *""",
            (handle, contact_id, relationship, creator_type, sorted(uncertainties), row["id"]),
        ).fetchone()
        return _finish(conn, scope, updated, request_id, digest, "draft_saved", row)


def _promote(conn: Any, row: dict, scope: dict, source: str) -> dict:
    if not all(row.get(k) for k in ("responsible_authorization_id", "contact_id", "relationship")):
        raise WorkflowInputError("请先确定负责人和对应 Lark 用户。")
    if row["uncertainties"]:
        raise WorkflowInputError("还有未核对清楚的信息，请先保存核对结果。")
    _lock_contact_scope(conn, row["responsible_authorization_id"], row["contact_id"])
    if _conflicts(conn, row, manual=True):
        raise WorkflowInputError("现有达人归属或账号关联有冲突，已保留待确认，不会覆盖旧关系。")
    account = conn.execute(
        "SELECT id FROM social_accounts WHERE platform=%s AND normalized_handle=%s FOR UPDATE",
        (row["platform"], row["normalized_handle"]),
    ).fetchone() if row["normalized_handle"] else None
    if account:
        conn.execute(
            "SELECT * FROM contact_social_accounts WHERE social_account_id=%s FOR UPDATE", (account["id"],)
        ).fetchall()
    # Reuse an existing person entry, including when its first handle arrives later.
    current = conn.execute(
        """SELECT * FROM creator_account_assignments WHERE ended_at IS NULL AND (
          candidate_id=%s OR social_account_id=%s OR
          (contact_id=%s AND (%s::text IS NULL OR social_account_id IS NULL)))
          ORDER BY (candidate_id=%s) DESC,started_at,id LIMIT 1 FOR UPDATE""",
        (row["id"], account["id"] if account else None, row["contact_id"], row["normalized_handle"], row["id"]),
    ).fetchone()
    if current and current["candidate_id"] != row["id"]:
        known = conn.execute("SELECT * FROM creator_workflow_candidates WHERE id=%s FOR UPDATE",
                             (current["candidate_id"],)).fetchone()
        conn.execute("UPDATE creator_workflow_sources SET candidate_id=%s WHERE candidate_id=%s",
                     (known["id"], row["id"]))
        conn.execute(
            "UPDATE creator_workflow_candidates SET status='merged',merged_into=%s,"
            "version=version+1,updated_at=NOW() WHERE id=%s", (known["id"], row["id"]),
        )
        row = {**known, "platform": known["platform"] if known["normalized_handle"] else row["platform"],
               "normalized_handle": known["normalized_handle"] or row["normalized_handle"],
               "creator_type": known.get("creator_type") or row.get("creator_type")}
        if known["social_account_id"]:
            account = {"id": known["social_account_id"]}
    if row["normalized_handle"] and not account:
        account = conn.execute(
            """INSERT INTO social_accounts(id,platform,normalized_handle,display_handle)
            VALUES(%s,%s,%s,%s) RETURNING id""",
            (uuid4(), row["platform"], row["normalized_handle"], "@" + row["normalized_handle"]),
        ).fetchone()
    elif account:
        # Reuse an established relationship without inventing a new owner claim.
        known = conn.execute(
            """SELECT relationship FROM contact_social_accounts
            WHERE social_account_id=%s AND contact_id=%s AND status='confirmed'
            ORDER BY relationship='owner' DESC,relationship LIMIT 1""",
            (account["id"], row["contact_id"]),
        ).fetchone()
        if known:
            row = {**row, "relationship": known["relationship"]}
    account_id = account["id"] if account else None
    if account:
        conn.execute(
            """INSERT INTO contact_social_accounts(contact_id,social_account_id,relationship,status,source,
          reviewed_by_authorization_id,reviewed_at)
        VALUES(%s,%s,%s,'confirmed',%s,%s,NOW())
        ON CONFLICT(contact_id,social_account_id,relationship) DO UPDATE SET status='confirmed',source=EXCLUDED.source,
          reviewed_by_authorization_id=EXCLUDED.reviewed_by_authorization_id,reviewed_at=NOW(),updated_at=NOW()
        WHERE contact_social_accounts.status <> 'confirmed'""",
            (row["contact_id"], account_id, row["relationship"], "creator_workflow_" + source, scope["actor_id"]),
        )
    if current:
        conn.execute(
            "UPDATE creator_account_assignments SET social_account_id=%s,creator_type=%s,relationship=%s WHERE id=%s",
            (account_id, row.get("creator_type"), row["relationship"], current["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO creator_account_assignments(id,social_account_id,responsible_authorization_id,contact_id,
            relationship,source,candidate_id,created_by_authorization_id,creator_type)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid4(), account_id, row["responsible_authorization_id"], row["contact_id"], row["relationship"],
             source, row["id"], scope["actor_id"], row.get("creator_type")),
        )
    # The legacy person-role/assignment system stays separate.
    return conn.execute(
        """UPDATE creator_workflow_candidates SET status='confirmed',social_account_id=%s,
        normalized_handle=%s,platform=%s,creator_type=%s,relationship=%s,
        version=version+1,updated_at=NOW() WHERE id=%s RETURNING *""",
        (account_id, row["normalized_handle"], row["platform"], row.get("creator_type"),
         row["relationship"], row["id"]),
    ).fetchone()


def confirm_candidate(scope: dict, candidate_id: UUID, version: int, *, request_id: UUID) -> dict:
    with connection() as conn, conn.transaction():
        scope = _fresh(conn, scope, write=True)
        request_id, digest, replay = _request(
            conn, scope, request_id, {"action": "confirm", "id": candidate_id, "version": version}
        )
        if replay:
            return _record(conn, _candidate(conn, replay["candidate_id"], scope), scope)
        original = _candidate(conn, candidate_id, scope)
        if original["normalized_handle"]:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"creator-account:{original['platform']}:{original['normalized_handle']}",),
            )
        row = _candidate(conn, candidate_id, scope, lock=True)
        _version(row, version)
        _editable(row)
        if not all(row.get(k) for k in ("responsible_authorization_id", "contact_id")):
            raise WorkflowInputError("请先确定负责人和对应 Lark 用户。")
        # This explicit click confirms the displayed account/user pair. Historical
        # flags stay in source records and the audit's before snapshot, not a checklist.
        reviewed = conn.execute(
            """UPDATE creator_workflow_candidates SET relationship='associated',
            uncertainties='{}' WHERE id=%s RETURNING *""", (row["id"],),
        ).fetchone()
        updated = _promote(conn, reviewed, scope, "cm_confirmation")
        return _finish(conn, scope, updated, request_id, digest, "confirmed", row)


def decide_candidate(scope: dict, candidate_id: UUID, version: int, action: str, *, request_id: UUID) -> dict:
    if action not in {"reject", "defer"}:
        raise WorkflowInputError("无效的确认操作。")
    with connection() as conn, conn.transaction():
        scope = _fresh(conn, scope, write=True)
        request_id, digest, replay = _request(
            conn, scope, request_id, {"action": action, "id": candidate_id, "version": version}
        )
        if replay:
            # A denied candidate no longer belongs to the CM, but its exact receipt remains safe to show.
            row = conn.execute(
                "SELECT * FROM creator_workflow_candidates WHERE id=%s", (replay["candidate_id"],)
            ).fetchone()
            return _record(conn, row, {**scope, "can_write": False})
        row = _candidate(conn, candidate_id, scope, lock=True)
        _version(row, version)
        _editable(row)
        updated = conn.execute(
            """UPDATE creator_workflow_candidates SET status=%s,
            responsible_authorization_id=CASE WHEN %s='reject' THEN NULL ELSE responsible_authorization_id END,
            uncertainties=CASE WHEN %s='reject' THEN array_append(uncertainties,'ownership_uncertain')
              ELSE uncertainties END,
            version=version+1,updated_at=NOW() WHERE id=%s RETURNING *""",
            ("rejected" if action == "reject" else "deferred", action, action, row["id"]),
        ).fetchone()
        return _finish(conn, scope, updated, request_id, digest, action, row)


def assign_candidate(
    scope: dict, candidate_id: UUID, version: int, responsible_id: UUID | None, *, request_id: UUID
) -> dict:
    responsible_id = _uuid(responsible_id, optional=True)
    with connection() as conn, conn.transaction():
        scope = _fresh(conn, scope)
        if not scope["can_admin"]:
            raise PermissionError("只有系统主人可以分派待确认记录。")
        scope = _scope(conn, _uuid(scope["actor_id"]), None, lock=True)
        request_id, digest, replay = _request(
            conn, scope, request_id, {"action": "assign", "id": candidate_id, "version": version, "cm": responsible_id}
        )
        if replay:
            return _record(conn, _candidate(conn, replay["candidate_id"], scope), scope)
        row = _candidate(conn, candidate_id, scope, lock=True)
        _version(row, version)
        if row["status"] == "confirmed":
            raise WorkflowInputError("正式账号不能通过候选分派变更负责人。")
        if responsible_id:
            cm = conn.execute(
                "SELECT id FROM authorizations WHERE id=%s AND status='active' AND assignment_eligible FOR SHARE",
                (responsible_id,),
            ).fetchone()
            if not cm:
                raise WorkflowInputError("请选择有效的 CM 负责人。")
        updated = conn.execute(
            """UPDATE creator_workflow_candidates SET responsible_authorization_id=%s,
            status='pending',version=version+1,updated_at=NOW() WHERE id=%s RETURNING *""",
            (responsible_id, row["id"]),
        ).fetchone()
        return _finish(conn, scope, updated, request_id, digest, "assigned_for_review", row)


def _uncertainties(values: Any) -> list[str]:
    if (
        not isinstance(values, list | tuple)
        or len(values) > 30
        or any(not isinstance(v, str) or not 1 <= len(v) <= 300 for v in values)
    ):
        raise WorkflowInputError("待核对项格式不正确。")
    source_reasons = {
        "ownership_conflict": "ownership_uncertain",
        "identity_conflict": "identity_uncertain",
        "contact_unavailable": "identity_uncertain",
    }
    return sorted({source_reasons.get(v, v) for v in values if v not in COMPUTED_REASONS or v in source_reasons})


def _evidence_revision(conn: Any, refs: list[dict]) -> str:
    """Do not reuse edited/rebound evidence; ignore randomized encryption refreshes."""
    values = []
    for ref in refs:
        row = conn.execute(
            """SELECT m.message_update_time,m.sender_open_id,m.parent_id,m.root_id,m.thread_id,m.mentions_json,
                      i.contact_id,i.tenant_classification,c.name,c.chat_mode,c.chat_status,ac.p2p_counterpart_open_id
            FROM chat_messages m JOIN chats c ON c.chat_id=m.chat_id
            LEFT JOIN lark_identities i ON i.open_id=m.sender_open_id
            LEFT JOIN authorization_chats ac ON ac.chat_id=m.chat_id AND ac.authorization_id=%s
            WHERE m.chat_id=%s AND m.message_id=%s""",
            (ref["authorization_id"], ref["chat_id"], ref["message_id"]),
        ).fetchone()
        values.append([ref, dict(row) if row else None])
    return _digest(values)


def _maybe_auto_confirm(conn: Any, row: dict, scope: dict) -> dict:
    """A verified business dialogue can establish the person before their handle is known."""
    if (
        row["status"] != "pending"
        or not all(row.get(k) for k in ("responsible_authorization_id", "contact_id"))
        or set(row["uncertainties"]) - {"ai_review", "handle_uncertain"}
        or _conflicts(conn, {**row, "normalized_handle": None}, manual=True)
    ):
        return row
    source = conn.execute(
        "SELECT source_data FROM creator_workflow_sources WHERE candidate_id=%s AND source_type='ai' "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if not source:
        return row
    data = source["source_data"]
    scores = [data.get(k) for k in ("creator_confidence", "cm_confidence")]
    if (
        data.get("preview_only") is True
        or data.get("business_policy_version") != BUSINESS_POLICY_VERSION
        or data.get("proposed_cm_id") != str(row["responsible_authorization_id"])
        or data.get("proposed_contact_id") != str(row["contact_id"])
        or not all(type(score) in {int, float} and math.isfinite(score)
                   and AUTO_CONFIRM_THRESHOLD <= score <= 1 for score in scores)
        or not all(data.get(k) is True for k in
                   ("verified_business_dialogue", "direct_creator_evidence", "direct_cm_evidence"))
        or data.get("unknowns")
    ):
        return row
    if conn.execute(
        """SELECT 1 FROM audit_events WHERE action='creator_handle_identity_review_flagged'
        AND metadata->>'contact_id'=%s AND (metadata->>'handle'=%s OR metadata->>'handle' IS NULL) LIMIT 1""",
        (str(row["contact_id"]), row["normalized_handle"]),
    ).fetchone():
        return row
    try:
        refs = validate_evidence(conn, row["responsible_authorization_id"], data.get("evidence"))
    except (WorkflowInputError, PermissionError):
        return row
    if data.get("evidence_revision") != _evidence_revision(conn, refs):
        return row
    handle = row["normalized_handle"]
    if (
        not data.get("exact_handle_evidence") or data.get("proposed_handle") != handle
        or _conflicts(conn, row, manual=True)
    ):
        # Establishing a person does not establish an unverified/conflicting
        # social account. Keep that proposal in its source for later CM review.
        handle = None
    reviewed = conn.execute(
        "UPDATE creator_workflow_candidates SET uncertainties='{}',relationship='associated',normalized_handle=%s "
        "WHERE id=%s RETURNING *", (handle, row["id"]),
    ).fetchone()
    return _promote(conn, reviewed, scope, "ai_business_verified")


def ingest_source(
    actor_id: UUID,
    *,
    source_type: str,
    source_key: str,
    source_data: dict,
    responsible_id: UUID | None = None,
    platform: str = "tiktok",
    handle: str | None = None,
    contact_id: UUID | None = None,
    relationship: str | None = None,
    creator_type: str | None = None,
    uncertainties: list[str] | tuple = (),
    trusted_identity: bool = False,
    request_id: UUID | None = None,
    conn: Any = None,
) -> dict:
    """Server-side ingestion; legacy confidence/import flags do not prove a business relationship."""
    if source_type not in {"excel", "ai"} or not isinstance(source_key, str) or not 1 <= len(source_key) <= 500:
        raise WorkflowInputError("来源类型或来源编号不正确。")
    if not isinstance(source_data, dict) or len(json.dumps(source_data, default=str)) > 100_000:
        raise WorkflowInputError("来源资料格式不正确或过大。")
    if relationship not in {None, "owner", "representative"} or platform not in {"tiktok", "instagram", "youtube"}:
        raise WorkflowInputError("平台或用户关系不正确。")
    handle, contact_id = _handle(handle), _uuid(contact_id, optional=True)
    if source_type == "ai" and not handle and not contact_id:
        raise WorkflowInputError("AI 核对必须指向具体用户；没有用户和 handle 的结果不能成为达人记录。")
    creator_type = _creator_type(creator_type)
    responsible_id = _uuid(responsible_id, optional=True)
    actor_id = _uuid(actor_id)
    unknowns = _uncertainties(uncertainties)
    if source_type == "ai":
        unknowns = sorted(set(unknowns) | {"ai_review"})
    source_data = dict(source_data)
    source_data.update(
        {
            "proposed_handle": handle,
            "proposed_cm_id": str(responsible_id) if responsible_id else None,
            "proposed_contact_id": str(contact_id) if contact_id else None,
            "proposed_relationship": relationship,
            "proposed_creator_type": creator_type,
            "proposed_platform": platform,
        }
    )
    envelope = {
        "source_data": source_data,
        "cm": responsible_id,
        "handle": handle,
        "contact": contact_id,
        "platform": platform,
        "relationship": relationship,
        "creator_type": creator_type,
        "uncertainties": unknowns,
    }
    if source_type == "ai":
        envelope["source_data"] = {key: source_data.get(key) for key in (
            "evidence", "cap_policy_version", "creator_confidence", "identity_confidence", "cm_confidence",
            "exact_handle_evidence", "direct_creator_evidence", "direct_cm_evidence", "unknowns",
            "business_policy_version", "verified_business_dialogue", "preview_only",
        )}
    fingerprint = _digest(envelope)
    with nullcontext(conn) if conn is not None else connection() as active, active.transaction():
        scope = _scope(active, actor_id, responsible_id or actor_id, lock=True)
        if not scope["can_import"] or responsible_id and responsible_id != actor_id and not scope["is_owner"]:
            raise PermissionError("不能为其他 CM 导入候选名单。")
        if responsible_id:
            cm = active.execute(
                "SELECT id FROM authorizations WHERE id=%s AND status='active' AND assignment_eligible FOR SHARE",
                (responsible_id,),
            ).fetchone()
            if not cm:
                raise WorkflowInputError("来源中的负责人不是当前有效的 CM。")
        if source_type == "ai":
            source_data["evidence"] = validate_evidence(
                active, responsible_id or (None if scope["is_owner"] else actor_id), source_data.get("evidence")
            )
            source_data["evidence_revision"] = _evidence_revision(active, source_data["evidence"])
            envelope["source_data"]["evidence_revision"] = source_data["evidence_revision"]
            fingerprint = _digest(envelope)
        if contact_id and responsible_id:
            _lock_contact_scope(active, responsible_id, contact_id)
        elif contact_id:
            # Unassigned records cannot expose a population-library identity as though it were selected by a CM.
            source_data["unassigned_contact_id"] = str(contact_id)
            contact_id = None
            unknowns = sorted(set(unknowns) | {"identity_uncertain"})
        active.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"creator-source:{source_type}:{source_key}",)
        )
        if handle:
            active.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"creator-account:{platform}:{handle}",)
            )
        existing_source = active.execute(
            "SELECT candidate_id FROM creator_workflow_sources "
            "WHERE source_type=%s AND source_key=%s AND fingerprint=%s",
            (source_type, source_key, fingerprint),
        ).fetchone()
        view_scope = {**scope, "responsible_id": None} if scope["is_owner"] else scope
        if existing_source:
            existing = active.execute(
                "SELECT * FROM creator_workflow_candidates WHERE id=%s", (existing_source["candidate_id"],)
            ).fetchone()
            return _record(active, existing, view_scope)
        row = (
            active.execute(
                """SELECT * FROM creator_workflow_candidates WHERE platform=%s AND normalized_handle=%s
            AND merged_into IS NULL ORDER BY (status='confirmed'),updated_at DESC LIMIT 1 FOR UPDATE""",
                (platform, handle),
            ).fetchone()
            if handle
            else None
        )
        if not row and contact_id and responsible_id:
            row = active.execute(
                """SELECT * FROM creator_workflow_candidates WHERE contact_id=%s AND responsible_authorization_id=%s
                AND merged_into IS NULL AND status IN ('pending','deferred','confirmed')
                AND (normalized_handle IS NULL OR %s::text IS NULL OR (platform=%s AND normalized_handle=%s))
                ORDER BY (status='confirmed') DESC,updated_at DESC LIMIT 1 FOR UPDATE""",
                (contact_id, responsible_id, handle, platform, handle),
            ).fetchone()
        if not row:
            old_source = active.execute(
                "SELECT candidate_id FROM creator_workflow_sources WHERE source_type=%s AND source_key=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (source_type, source_key),
            ).fetchone()
            if old_source:
                row = active.execute(
                    "SELECT * FROM creator_workflow_candidates WHERE id=%s FOR UPDATE", (old_source["candidate_id"],)
                ).fetchone()
        if (
            row
            and row["status"] == "confirmed"
            and (
                handle and row["normalized_handle"]
                and row["normalized_handle"] != handle
                or responsible_id
                and row["responsible_authorization_id"] != responsible_id
                or contact_id
                and row["contact_id"] != contact_id
                or relationship
                and row["relationship"] not in {"associated", relationship}
                or creator_type and row.get("creator_type") and row["creator_type"] != creator_type
            )
        ):
            # Preserve the formal row and assignment. A changed proposal is a separate
            # administrator review, never an implicit handover or identity rewrite.
            source_data["proposed_responsible_id"] = str(responsible_id) if responsible_id else None
            source_data["existing_candidate_id"] = str(row["id"])
            responsible_id = None
            unknowns = sorted(set(unknowns) | {"source_conflict"})
            row = None
        if not row:
            row = active.execute(
                """INSERT INTO creator_workflow_candidates(id,platform,normalized_handle,responsible_authorization_id,
                contact_id,relationship,creator_type,uncertainties) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (uuid4(), platform, handle, responsible_id, contact_id, relationship, creator_type, unknowns),
            ).fetchone()
        elif row["status"] == "confirmed":
            # Exact known identity/CM only: imports may supply a first account or
            # CAP declaration, never silently replace an existing fact.
            proposed = {**row, "normalized_handle": row["normalized_handle"] or handle,
                        "platform": row["platform"] if row["normalized_handle"] or not handle else platform,
                        "creator_type": row.get("creator_type") or creator_type}
            if source_data.get("preview_only") is not True and (
                proposed["normalized_handle"], proposed.get("creator_type")
            ) != (row["normalized_handle"], row.get("creator_type")
            ):
                row = _promote(active, proposed, scope, "cm_confirmation")
        else:
            differences = (
                row["normalized_handle"]
                and handle
                and row["normalized_handle"] != handle
                or row["contact_id"]
                and contact_id
                and row["contact_id"] != contact_id
                or row["responsible_authorization_id"]
                and responsible_id
                and row["responsible_authorization_id"] != responsible_id
                or row["relationship"]
                and relationship
                and row["relationship"] != relationship
                or row.get("creator_type") and creator_type and row["creator_type"] != creator_type
            )
            merged_unknowns = sorted(
                set(row["uncertainties"]) | set(unknowns) | ({"source_conflict"} if differences else set())
            )
            different_cm = bool(
                row["responsible_authorization_id"]
                and responsible_id
                and row["responsible_authorization_id"] != responsible_id
            )
            # New material evidence may reopen a denial, but it goes to the administrator, never the next guessed CM.
            row = active.execute(
                """UPDATE creator_workflow_candidates SET
                platform=CASE WHEN normalized_handle IS NULL AND %s::text IS NOT NULL THEN %s ELSE platform END,
                normalized_handle=COALESCE(normalized_handle,%s),
                contact_id=COALESCE(contact_id,%s),relationship=COALESCE(relationship,%s),
                creator_type=COALESCE(creator_type,%s),uncertainties=%s,
                status=CASE WHEN status='deferred' THEN 'deferred' ELSE 'pending' END,
                responsible_authorization_id=CASE WHEN %s OR status='rejected'
                  OR (responsible_authorization_id IS NULL
                    AND uncertainties && ARRAY['source_conflict','ownership_uncertain']) THEN NULL
                  ELSE COALESCE(responsible_authorization_id,%s) END,
                version=version+1,updated_at=NOW()
                WHERE id=%s RETURNING *""",
                (handle, platform, handle, contact_id, relationship, creator_type, merged_unknowns,
                 different_cm, responsible_id, row["id"]),
            ).fetchone()
        active.execute(
            """INSERT INTO creator_workflow_sources(id,candidate_id,source_type,source_key,source_data,fingerprint,
              submitted_by_authorization_id,created_at)
            VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,clock_timestamp())""",
            (
                uuid4(),
                row["id"],
                source_type,
                source_key,
                json.dumps(source_data, ensure_ascii=False, default=str),
                fingerprint,
                actor_id,
            ),
        )
        was_confirmed = row["status"] == "confirmed"
        row = _maybe_auto_confirm(active, row, scope)
        promoted = not was_confirmed and row["status"] == "confirmed"
        _audit(
            active,
            actor_id,
            "source_imported",
            {
                "candidate_id": row["id"],
                "source_type": source_type,
                "source_key": source_key,
                "fingerprint": fingerprint,
                "ai_auto_confirmed": promoted,
                "business_policy_version": BUSINESS_POLICY_VERSION,
            },
        )
        return _record(active, row, view_scope)

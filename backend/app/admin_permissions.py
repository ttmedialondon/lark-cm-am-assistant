"""Explicit read-only admin page grants; only the existing owner can edit them."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID, uuid4

from .db import connection

PAGES = (
    {"key": "overview", "label": "概览", "path": "/admin", "description": "查看系统接入和同步概况"},
    {"key": "users", "label": "人员", "path": "/admin/users", "description": "查看已接入人员及其后台权限"},
    {"key": "chats", "label": "群聊", "path": "/admin/chats", "description": "查看会话目录、成员和同步信息"},
    {
        "key": "messages", "label": "消息内容", "path": "/admin/messages",
        "description": "所有已授权人员的可用聊天记录（含非达人）",
    },
    {"key": "contacts", "label": "人员目录", "path": "/admin/contacts", "description": "查看联系人身份与关联资料"},
    {"key": "ai", "label": "AI 功能", "path": "/admin/ai", "description": "查看 AI 功能与运行配置，不可修改"},
    {"key": "jobs", "label": "同步任务", "path": "/admin/jobs", "description": "查看同步任务及运行结果"},
    {
        "key": "creator-roster", "label": "跨 CM 达人名单", "path": "/creators",
        "description": "通过负责人筛选查看各 CM 已入库的达人名单",
    },
)
PAGE_KEYS = frozenset(page["key"] for page in PAGES)


def _access(row: Mapping[str, Any] | None) -> dict[str, Any]:
    active = bool(row and row["status"] == "active")
    owner_account = bool(row and row.get("role") == "owner")
    owner = bool(active and owner_account and row["role_active"])
    configured = frozenset(row.get("permissions") or ()) & PAGE_KEYS if row else frozenset()
    permissions = PAGE_KEYS if owner else configured if active and not owner_account else frozenset()
    return {
        "is_owner": owner,
        "message_source_id": row.get("message_source_id") if row and not owner else None,
        "is_owner_account": owner_account,
        "permissions": permissions,
        "configured_permissions": PAGE_KEYS if owner else configured,
        "landing_path": next((page["path"] for page in PAGES if page["key"] in permissions), None),
        "has_admin_access": bool(permissions),
        "access_label": (
            "系统主人 · 全部权限" if owner else f"可查看 {len(permissions)} 个页面" if permissions else "仅前台"
        ),
        "permission_labels": [page["label"] for page in PAGES if page["key"] in permissions],
    }


def users_access_data(authorization_ids: Iterable[UUID]) -> dict[UUID, dict[str, Any]]:
    """Load effective access and saved checkbox values in one query for the personnel page."""

    ids = sorted(set(authorization_ids), key=str)
    if not ids:
        return {}
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.status, r.role, r.is_active AS role_active,
                   (SELECT source_authorization_id FROM admin_message_source_limits
                    WHERE authorization_id = a.id) AS message_source_id,
                   COALESCE(array_agg(DISTINCT p.permission)
                       FILTER (WHERE p.permission IS NOT NULL), '{}'::text[]) AS permissions
            FROM authorizations a
            LEFT JOIN admin_roles r ON r.authorization_id = a.id
            LEFT JOIN admin_page_permissions p ON p.authorization_id = a.id
            WHERE a.id = ANY(%s::uuid[])
            GROUP BY a.id, r.role, r.is_active
            """,
            (ids,),
        ).fetchall()
    results = {authorization_id: _access(None) for authorization_id in ids}
    results.update({row["id"]: _access(row) for row in rows})
    return results


def access_for(authorization_id: UUID) -> dict[str, Any]:
    """Return current access; neither legacy viewer nor operator implies any page grant."""

    return users_access_data([authorization_id])[authorization_id]


def permission_keys_for(authorization_id: UUID) -> frozenset[str]:
    return access_for(authorization_id)["permissions"]


def set_permissions(actor_id: UUID, target_id: UUID, keys: Iterable[str]) -> None:
    """Atomically replace one non-owner's grants after rechecking the owner's live authority."""

    values = list(keys)
    if len(values) > len(PAGE_KEYS):
        raise ValueError("后台页面权限数量超过可选范围。")
    if any(not isinstance(key, str) or key not in PAGE_KEYS for key in values):
        raise ValueError("包含无效的后台页面权限。")
    if len(set(values)) != len(values):
        raise ValueError("后台页面权限不能重复。")
    requested = sorted(values)
    ids = sorted({actor_id, target_id}, key=str)
    with connection() as conn, conn.transaction():
        # Lock both accounts in a stable order so concurrent saves cannot mix their grants.
        accounts = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, status FROM authorizations WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE",
                (ids,),
            ).fetchall()
        }
        roles = {
            row["authorization_id"]: row
            for row in conn.execute(
                """
                SELECT authorization_id, role, is_active FROM admin_roles
                WHERE authorization_id = ANY(%s::uuid[]) ORDER BY authorization_id FOR UPDATE
                """,
                (ids,),
            ).fetchall()
        }
        actor_role = roles.get(actor_id, {})
        if (
            accounts.get(actor_id, {}).get("status") != "active"
            or actor_role.get("role") != "owner"
            or not actor_role.get("is_active")
        ):
            raise PermissionError("只有当前有效的系统主人可以设置后台权限。")
        if target_id not in accounts:
            raise LookupError("找不到该人员。")
        if roles.get(target_id, {}).get("role") == "owner":
            raise ValueError("系统主人的权限固定为全部权限，不能通过此页面修改。")

        before = sorted(
            row["permission"]
            for row in conn.execute(
                "SELECT permission FROM admin_page_permissions WHERE authorization_id = %s FOR UPDATE",
                (target_id,),
            ).fetchall()
        )
        if before == requested:
            return
        conn.execute("DELETE FROM admin_page_permissions WHERE authorization_id = %s", (target_id,))
        if requested:
            conn.execute(
                """
                INSERT INTO admin_page_permissions (authorization_id, permission, granted_by)
                SELECT %s, permission, %s FROM unnest(%s::text[]) AS permission
                """,
                (target_id, actor_id, requested),
            )
        conn.execute(
            "INSERT INTO audit_events (id, authorization_id, action, metadata) VALUES (%s, %s, %s, %s::jsonb)",
            (
                uuid4(),
                actor_id,
                "admin_page_permissions_updated",
                json.dumps({"target_authorization_id": str(target_id), "before": before, "after": requested}),
            ),
        )

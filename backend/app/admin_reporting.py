"""Read-only reporting queries for the internal administration console.

The application keeps two deliberate views of a Lark group:

* ``chats`` and ``chat_messages`` are canonical, de-duplicated records.
* ``authorization_chats`` and ``authorization_message_observations`` record
  what a particular CM/AM was permitted to see.

Keeping that boundary here makes the admin UI independent from OAuth and HTTP
handling, and prevents company-wide totals from accidentally adding the same
group or message once per authorised user.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import connection

LONDON_TIME_ZONE = ZoneInfo("Europe/London")
MAX_LIST_LIMIT = 500


def admin_role_for(authorization_id: UUID) -> str | None:
    """Return an active role for an authorization, without exposing secrets."""

    with connection() as conn:
        row = conn.execute(
            """
            SELECT role
            FROM admin_roles
            WHERE authorization_id = %s AND is_active
            """,
            (authorization_id,),
        ).fetchone()
    return row["role"] if row else None


def overview() -> dict[str, Any]:
    """Return small, de-duplicated summary data for the admin home page."""

    with connection() as conn:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM authorizations WHERE status = 'active') AS active_authorization_count,
                (SELECT COUNT(*) FROM chats
                  WHERE COALESCE(chat_status, 'normal') = 'normal') AS chat_count,
                (SELECT COUNT(*)
                   FROM authorization_chats ac
                   JOIN chats c ON c.chat_id = ac.chat_id
                  WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal') AS membership_count,
                (SELECT COUNT(*) FROM chat_messages WHERE NOT is_deleted) AS message_count,
                (SELECT COUNT(*)
                   FROM authorization_chats ac
                   JOIN chats c ON c.chat_id = ac.chat_id
                  WHERE ac.is_visible
                    AND COALESCE(c.chat_status, 'normal') = 'normal'
                    AND ac.sync_status IN ('pending', 'syncing', 'failed_retryable')) AS pending_membership_count,
                (SELECT COUNT(*) FROM sync_jobs WHERE status IN ('queued', 'running')) AS active_job_count
            """
        ).fetchone()
        attention = conn.execute(
            """
            WITH group_counts AS (
                SELECT
                    ac.authorization_id,
                    COUNT(*) AS group_count,
                    COUNT(*) FILTER (
                        WHERE ac.sync_status IN ('pending', 'syncing', 'failed_retryable')
                    ) AS pending_group_count,
                    MAX(COALESCE(ac.last_incremental_at, ac.last_backfill_at)) AS last_synced_at
                FROM authorization_chats ac
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal'
                GROUP BY ac.authorization_id
            ), latest_jobs AS (
                SELECT DISTINCT ON (authorization_id)
                    authorization_id, status, updated_at
                FROM sync_jobs
                ORDER BY authorization_id, created_at DESC
            )
            SELECT
                a.id,
                a.display_name,
                a.status AS authorization_status,
                COALESCE(g.group_count, 0) AS group_count,
                COALESCE(g.pending_group_count, 0) AS pending_group_count,
                g.last_synced_at,
                j.status AS job_status,
                j.updated_at AS job_updated_at
            FROM authorizations a
            LEFT JOIN group_counts g ON g.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            WHERE a.status <> 'active'
               OR COALESCE(g.pending_group_count, 0) > 0
               OR j.status IN ('failed', 'attention_required')
            ORDER BY
                CASE WHEN a.status <> 'active' THEN 0 ELSE 1 END,
                g.last_synced_at NULLS FIRST,
                a.authorized_at DESC
            LIMIT 12
            """
        ).fetchall()
        recent_jobs = conn.execute(
            """
            SELECT
                j.id,
                j.mode,
                j.status,
                j.pages_processed,
                j.chats_discovered,
                j.messages_indexed,
                j.created_at,
                j.started_at,
                j.finished_at,
                a.display_name
            FROM sync_jobs j
            JOIN authorizations a ON a.id = j.authorization_id
            ORDER BY j.created_at DESC
            LIMIT 12
            """
        ).fetchall()

    metrics = (
        {
            "label": "有效授权人员",
            "value": int(counts["active_authorization_count"] or 0),
            "hint": "当前 OAuth 状态为有效的 CM / AM",
        },
        {
            "label": "去重后的群聊",
            "value": int(counts["chat_count"] or 0),
            "hint": "同一群被多人看到也只算一个群",
        },
        {
            "label": "人员—群聊关系",
            "value": int(counts["membership_count"] or 0),
            "hint": "当前可见群聊在各授权用户下的关系数",
        },
        {
            "label": "去重后消息索引",
            "value": int(counts["message_count"] or 0),
            "hint": "不保存消息正文；同一消息只计一次",
        },
        {
            "label": "待处理群聊关系",
            "value": int(counts["pending_membership_count"] or 0),
            "hint": "等待首次回填、同步中或等待重试",
        },
        {
            "label": "活动同步任务",
            "value": int(counts["active_job_count"] or 0),
            "hint": "当前排队或运行中的 worker 任务",
        },
    )
    return {
        "metrics": metrics,
        "attention": [_decorate_user(row) for row in attention],
        "recent_jobs": [_decorate_job(row) for row in recent_jobs],
        "runtime": runtime_settings(),
    }


def runtime_settings() -> tuple[dict[str, str], ...]:
    """Expose operating values useful to an owner without exposing secrets."""

    settings = get_settings()
    history = "全部 API 可返回历史" if settings.sync_lookback_days == 0 else f"最近 {settings.sync_lookback_days} 天"
    return (
        {"label": "首次回填范围", "value": history},
        {"label": "增量同步周期", "value": _seconds_label(settings.sync_interval_seconds)},
        {"label": "增量重叠窗口", "value": _seconds_label(settings.sync_overlap_seconds)},
        {"label": "每任务最大分页", "value": f"{settings.max_pages_per_job} 页"},
        {"label": "成员数刷新周期", "value": _seconds_label(settings.member_count_refresh_seconds)},
    )


def users(limit: int = 200) -> list[dict[str, Any]]:
    """Return per-user statistics without double-counting canonical messages."""

    with connection() as conn:
        rows = conn.execute(
            """
            WITH group_counts AS (
                SELECT
                    ac.authorization_id,
                    COUNT(*) AS group_count,
                    COUNT(*) FILTER (
                        WHERE ac.sync_status IN ('pending', 'syncing', 'failed_retryable')
                    ) AS pending_group_count,
                    MAX(COALESCE(ac.last_incremental_at, ac.last_backfill_at)) AS last_synced_at
                FROM authorization_chats ac
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal'
                GROUP BY ac.authorization_id
            ), message_counts AS (
                SELECT
                    o.authorization_id,
                    COUNT(*) FILTER (WHERE NOT m.is_deleted) AS message_count,
                    COUNT(*) FILTER (WHERE o.sent_by_author AND NOT m.is_deleted) AS own_message_count
                FROM authorization_message_observations o
                JOIN authorization_chats ac
                  ON ac.authorization_id = o.authorization_id AND ac.chat_id = o.chat_id
                JOIN chats c ON c.chat_id = ac.chat_id
                JOIN chat_messages m
                  ON m.chat_id = o.chat_id AND m.message_id = o.message_id
                WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal'
                GROUP BY o.authorization_id
            ), latest_jobs AS (
                SELECT DISTINCT ON (authorization_id)
                    authorization_id, mode, status, updated_at
                FROM sync_jobs
                ORDER BY authorization_id, created_at DESC
            )
            SELECT
                a.id,
                a.display_name,
                a.status AS authorization_status,
                a.authorized_at,
                a.updated_at,
                r.role AS admin_role,
                COALESCE(g.group_count, 0) AS group_count,
                COALESCE(g.pending_group_count, 0) AS pending_group_count,
                g.last_synced_at,
                COALESCE(m.message_count, 0) AS message_count,
                COALESCE(m.own_message_count, 0) AS own_message_count,
                j.mode AS job_mode,
                j.status AS job_status,
                j.updated_at AS job_updated_at
            FROM authorizations a
            LEFT JOIN admin_roles r ON r.authorization_id = a.id AND r.is_active
            LEFT JOIN group_counts g ON g.authorization_id = a.id
            LEFT JOIN message_counts m ON m.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            ORDER BY a.authorized_at DESC
            LIMIT %s
            """,
            (_list_limit(limit),),
        ).fetchall()
    return [_decorate_user(row) for row in rows]


def user_detail(authorization_id: UUID) -> dict[str, Any] | None:
    """Return one user plus their current group relationships and recent jobs."""

    with connection() as conn:
        user = conn.execute(
            """
            WITH group_counts AS (
                SELECT
                    ac.authorization_id,
                    COUNT(*) AS group_count,
                    COUNT(*) FILTER (
                        WHERE ac.sync_status IN ('pending', 'syncing', 'failed_retryable')
                    ) AS pending_group_count,
                    MAX(COALESCE(ac.last_incremental_at, ac.last_backfill_at)) AS last_synced_at
                FROM authorization_chats ac
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.authorization_id = %s
                  AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                GROUP BY ac.authorization_id
            ), message_counts AS (
                SELECT
                    o.authorization_id,
                    COUNT(*) FILTER (WHERE NOT m.is_deleted) AS message_count,
                    COUNT(*) FILTER (WHERE o.sent_by_author AND NOT m.is_deleted) AS own_message_count
                FROM authorization_message_observations o
                JOIN authorization_chats ac
                  ON ac.authorization_id = o.authorization_id AND ac.chat_id = o.chat_id
                JOIN chats c ON c.chat_id = ac.chat_id
                JOIN chat_messages m
                  ON m.chat_id = o.chat_id AND m.message_id = o.message_id
                WHERE o.authorization_id = %s
                  AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                GROUP BY o.authorization_id
            ), latest_jobs AS (
                SELECT DISTINCT ON (authorization_id)
                    authorization_id, mode, status, updated_at
                FROM sync_jobs
                WHERE authorization_id = %s
                ORDER BY authorization_id, created_at DESC
            )
            SELECT
                a.id,
                a.display_name,
                a.status AS authorization_status,
                a.authorized_at,
                a.updated_at,
                r.role AS admin_role,
                COALESCE(g.group_count, 0) AS group_count,
                COALESCE(g.pending_group_count, 0) AS pending_group_count,
                g.last_synced_at,
                COALESCE(m.message_count, 0) AS message_count,
                COALESCE(m.own_message_count, 0) AS own_message_count,
                j.mode AS job_mode,
                j.status AS job_status,
                j.updated_at AS job_updated_at
            FROM authorizations a
            LEFT JOIN admin_roles r ON r.authorization_id = a.id AND r.is_active
            LEFT JOIN group_counts g ON g.authorization_id = a.id
            LEFT JOIN message_counts m ON m.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            WHERE a.id = %s
            """,
            (authorization_id, authorization_id, authorization_id, authorization_id),
        ).fetchone()
        if not user:
            return None

        groups = conn.execute(
            """
            SELECT
                ac.chat_id,
                COALESCE(c.name, '未命名群聊') AS name,
                c.external,
                ac.member_count,
                ac.member_count_status,
                ac.sync_status,
                ac.last_backfill_at,
                ac.last_incremental_at,
                COUNT(o.message_id) FILTER (WHERE NOT m.is_deleted) AS message_count
            FROM authorization_chats ac
            LEFT JOIN chats c ON c.chat_id = ac.chat_id
            LEFT JOIN authorization_message_observations o
              ON o.authorization_id = ac.authorization_id AND o.chat_id = ac.chat_id
            LEFT JOIN chat_messages m ON m.chat_id = o.chat_id AND m.message_id = o.message_id
            WHERE ac.authorization_id = %s
              AND ac.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
            GROUP BY
                ac.authorization_id, ac.chat_id, ac.member_count, ac.member_count_status,
                ac.sync_status,
                ac.last_backfill_at, ac.last_incremental_at,
                c.name, c.external
            ORDER BY LOWER(COALESCE(c.name, '')), ac.chat_id
            """,
            (authorization_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT
                id, mode, status, pages_processed, chats_discovered, messages_indexed,
                own_messages_indexed, attempts, created_at, started_at, finished_at
            FROM sync_jobs
            WHERE authorization_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (authorization_id,),
        ).fetchall()

    return {
        "user": _decorate_user(user),
        "groups": [_decorate_group_membership(row) for row in groups],
        "jobs": [_decorate_job(row) for row in jobs],
    }


def chats(limit: int = 500) -> list[dict[str, Any]]:
    """Return canonical groups and how many authorised users currently see each."""

    with connection() as conn:
        rows = conn.execute(
            """
            WITH memberships AS (
                SELECT
                    ac.chat_id,
                    COUNT(*) AS visible_authorization_count,
                    COUNT(*) FILTER (
                        WHERE ac.sync_status IN ('pending', 'syncing', 'failed_retryable')
                    ) AS pending_authorization_count,
                    MAX(COALESCE(ac.last_incremental_at, ac.last_backfill_at)) AS last_synced_at
                FROM authorization_chats ac
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal'
                GROUP BY ac.chat_id
            ), member_counts AS (
                SELECT DISTINCT ON (ac.chat_id)
                    ac.chat_id,
                    ac.member_count,
                    ac.member_count_status,
                    ac.member_count_checked_at
                FROM authorization_chats ac
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.is_visible
                  AND ac.sync_enabled
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                ORDER BY
                    ac.chat_id,
                    CASE WHEN ac.member_count_status = 'succeeded' THEN 0 ELSE 1 END,
                    ac.member_count_checked_at DESC NULLS LAST,
                    ac.last_seen_at DESC
            ), messages AS (
                SELECT chat_id, COUNT(*) FILTER (WHERE NOT is_deleted) AS message_count
                FROM chat_messages
                GROUP BY chat_id
            )
            SELECT
                c.chat_id,
                c.name,
                c.external,
                mc.member_count,
                mc.member_count_status,
                mc.member_count_checked_at,
                c.first_seen_at,
                c.last_seen_at,
                COALESCE(m.visible_authorization_count, 0) AS visible_authorization_count,
                COALESCE(m.pending_authorization_count, 0) AS pending_authorization_count,
                m.last_synced_at,
                COALESCE(msg.message_count, 0) AS message_count
            FROM chats c
            LEFT JOIN memberships m ON m.chat_id = c.chat_id
            LEFT JOIN member_counts mc ON mc.chat_id = c.chat_id
            LEFT JOIN messages msg ON msg.chat_id = c.chat_id
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
            ORDER BY LOWER(COALESCE(c.name, '')), c.chat_id
            LIMIT %s
            """,
            (_list_limit(limit),),
        ).fetchall()
    return [_decorate_chat(row) for row in rows]


def jobs(limit: int = 200) -> list[dict[str, Any]]:
    """Return recent worker jobs. Token material is never selected."""

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
                j.id,
                j.mode,
                j.status,
                j.pages_processed,
                j.chats_discovered,
                j.messages_indexed,
                j.own_messages_indexed,
                j.attempts,
                j.created_at,
                j.started_at,
                j.finished_at,
                j.updated_at,
                a.display_name
            FROM sync_jobs j
            JOIN authorizations a ON a.id = j.authorization_id
            ORDER BY j.created_at DESC
            LIMIT %s
            """,
            (_list_limit(limit),),
        ).fetchall()
    return [_decorate_job(row) for row in rows]


def _list_limit(value: int) -> int:
    return max(1, min(value, MAX_LIST_LIMIT))


def _decorate_user(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "未命名用户"
    for field in ("authorized_at", "updated_at", "last_synced_at", "job_updated_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    item["authorization_status_label"], item["authorization_status_badge"] = _authorization_status(
        item.get("authorization_status")
    )
    item["job_status_label"], item["job_status_badge"] = _job_status(item.get("job_status"))
    return item


def _decorate_group_membership(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["member_count_label"] = _member_count(item.get("member_count"), item.get("member_count_status"))
    item["sync_status_label"], item["sync_status_badge"] = _sync_status(item.get("sync_status"))
    item["last_synced_at_label"] = _format_time(item.get("last_incremental_at") or item.get("last_backfill_at"))
    return item


def _decorate_chat(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["name"] = item.get("name") or "未命名群聊"
    item["member_count_label"] = _member_count(item.get("member_count"), item.get("member_count_status"))
    for field in ("last_seen_at", "last_synced_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    return item


def _decorate_job(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "未命名用户"
    for field in ("created_at", "started_at", "finished_at", "updated_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    item["status_label"], item["status_badge"] = _job_status(item.get("status"))
    return item


def _format_time(value: datetime | None) -> str:
    if not value:
        return "尚未完成"
    return value.astimezone(LONDON_TIME_ZONE).strftime("%Y-%m-%d %H:%M %Z")


def _seconds_label(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _authorization_status(value: str | None) -> tuple[str, str]:
    mapping = {
        "active": ("有效", "success"),
        "attention_required": ("需要处理", "warning"),
        "revoked": ("已撤销", "secondary"),
    }
    return mapping.get(value or "", ("状态未知", "secondary"))


def _job_status(value: str | None) -> tuple[str, str]:
    mapping = {
        "queued": ("排队中", "secondary"),
        "running": ("运行中", "primary"),
        "succeeded": ("已完成", "success"),
        "failed": ("失败", "danger"),
        "attention_required": ("需要授权处理", "warning"),
        "cancelled": ("已取消", "secondary"),
    }
    return mapping.get(value or "", ("尚无任务", "secondary"))


def _sync_status(value: str | None) -> tuple[str, str]:
    mapping = {
        "pending": ("等待首次回填", "secondary"),
        "syncing": ("正在同步", "primary"),
        "succeeded": ("已同步", "success"),
        "not_readable": ("Lark 未允许读取", "warning"),
        "failed_retryable": ("等待重试", "warning"),
    }
    return mapping.get(value or "", ("状态未知", "secondary"))


def _member_count(count: int | None, status: str | None) -> str:
    if count is not None:
        return str(count)
    if status == "pending":
        return "正在获取"
    return "Lark 未返回"

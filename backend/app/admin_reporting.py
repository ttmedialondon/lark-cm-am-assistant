"""Read-only reporting queries for the internal administration console.

The application keeps two deliberate views of a Lark conversation:

* ``chats`` and ``chat_messages`` are canonical, de-duplicated records.
* ``authorization_chats`` and ``authorization_message_observations`` record
  what a particular CM/AM was permitted to see.

Keeping that boundary here makes the admin UI independent from OAuth and HTTP
handling, and prevents company-wide totals from accidentally adding the same
conversation or message once per authorised user.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag

from .config import get_settings
from .crypto import TokenCipher, message_body_cipher
from .db import connection

LONDON_TIME_ZONE = ZoneInfo("Europe/London")
LIST_PAGE_SIZE = 25
DETAIL_PAGE_SIZE = 10
MAX_PAGE = 10_000
MESSAGE_PAGE_SIZE = 100
MESSAGE_CONVERSATION_PAGE_SIZE = 5
MAX_MESSAGE_CONVERSATION_QUERY_LENGTH = 120
MESSAGE_CONVERSATION_TYPES = frozenset({"all", "group", "p2p"})
MESSAGE_CONVERSATION_DIRECTORY_SQL = """
    SELECT
        ac.authorization_id,
        ac.chat_id,
        c.name,
        c.external,
        COALESCE(c.chat_mode, 'group') AS chat_mode,
        counterpart.display_name AS counterpart_display_name,
        ac.member_count,
        ac.member_count_status,
        a.body_consent_at
    FROM authorizations a
    JOIN authorization_chats ac ON ac.authorization_id = a.id
    JOIN chats c ON c.chat_id = ac.chat_id
    LEFT JOIN lark_identities counterpart ON counterpart.open_id = ac.p2p_counterpart_open_id
    WHERE a.id = %s
      AND ac.is_visible
      AND COALESCE(c.chat_status, 'normal') = 'normal'
"""
MESSAGE_CONVERSATION_FILTER_SQL = """
      AND (
        %s = ''
        OR POSITION(LOWER(%s) IN LOWER(COALESCE(c.name, ''))) > 0
        OR POSITION(LOWER(%s) IN LOWER(COALESCE(counterpart.display_name, ''))) > 0
      )
      AND (
        %s = 'all'
        OR (%s = 'p2p' AND COALESCE(c.chat_mode, 'group') = 'p2p')
        OR (%s = 'group' AND COALESCE(c.chat_mode, 'group') <> 'p2p')
      )
"""
MESSAGE_CONVERSATION_COUNTS_SQL = """
    SELECT
        conversations.chat_id,
        conversations.name,
        conversations.external,
        conversations.chat_mode,
        conversations.counterpart_display_name,
        conversations.member_count,
        conversations.member_count_status,
        COUNT(o.message_id) FILTER (WHERE NOT m.is_deleted) AS message_count,
        COUNT(o.message_id) FILTER (
            WHERE NOT m.is_deleted
              AND m.body_ciphertext IS NOT NULL
              AND conversations.body_consent_at IS NOT NULL
              AND o.observed_at >= conversations.body_consent_at
        ) AS content_count
    FROM conversations
    LEFT JOIN authorization_message_observations o
      ON o.authorization_id = conversations.authorization_id AND o.chat_id = conversations.chat_id
    LEFT JOIN chat_messages m ON m.chat_id = o.chat_id AND m.message_id = o.message_id
    GROUP BY
        conversations.authorization_id, conversations.chat_id, conversations.name, conversations.external,
        conversations.chat_mode, conversations.counterpart_display_name, conversations.member_count,
        conversations.member_count_status, conversations.body_consent_at
"""
CONVERSATION_COUNTS_SQL = """
    SELECT
        ac.authorization_id,
        COUNT(*) AS discovered_conversation_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
        ) AS syncable_conversation_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') <> 'normal'
        ) AS archived_conversation_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND COALESCE(c.chat_mode, 'group') <> 'p2p'
        ) AS group_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND c.chat_mode = 'p2p'
        ) AS p2p_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND ac.backfill_complete
        ) AS backfill_complete_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND ac.sync_status = 'syncing'
        ) AS syncing_conversation_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND ac.sync_status IN ('pending', 'failed_retryable')
        ) AS waiting_conversation_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND ac.sync_status = 'not_readable'
        ) AS not_readable_conversation_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND c.chat_mode = 'p2p' AND ac.backfill_complete
        ) AS p2p_backfill_complete_count,
        COUNT(*) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
              AND COALESCE(c.chat_mode, 'group') <> 'p2p' AND ac.backfill_complete
        ) AS group_backfill_complete_count,
        MAX(COALESCE(ac.last_incremental_at, ac.last_backfill_at)) FILTER (
            WHERE COALESCE(c.chat_status, 'normal') = 'normal'
        ) AS last_synced_at
    FROM authorization_chats ac
    JOIN chats c ON c.chat_id = ac.chat_id
    WHERE ac.is_visible
    GROUP BY ac.authorization_id
"""


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
                  WHERE COALESCE(chat_status, 'normal') = 'normal'
                    AND COALESCE(chat_mode, 'group') <> 'p2p') AS group_chat_count,
                (SELECT COUNT(*) FROM chats
                  WHERE COALESCE(chat_status, 'normal') = 'normal'
                    AND chat_mode = 'p2p') AS p2p_chat_count,
                (SELECT COUNT(*)
                   FROM authorization_chats ac
                   JOIN chats c ON c.chat_id = ac.chat_id
                  WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal') AS membership_count,
                (SELECT COUNT(*) FROM chat_messages WHERE NOT is_deleted) AS message_count,
                (SELECT COUNT(*) FROM chat_messages
                  WHERE NOT is_deleted AND body_ciphertext IS NOT NULL) AS content_message_count,
                (SELECT COUNT(*)
                   FROM authorization_chats ac
                   JOIN chats c ON c.chat_id = ac.chat_id
                  WHERE ac.is_visible
                    AND COALESCE(c.chat_status, 'normal') = 'normal'
                    AND ac.sync_status IN ('pending', 'syncing', 'failed_retryable')) AS pending_membership_count,
                (SELECT COUNT(*) FROM sync_jobs
                  WHERE status = 'running'
                     OR (status = 'queued' AND scheduled_at <= NOW())) AS active_job_count
            """
        ).fetchone()
        attention = conn.execute(
            f"""
            WITH conversation_counts AS (
                {CONVERSATION_COUNTS_SQL}
            ), latest_jobs AS (
                SELECT DISTINCT ON (authorization_id)
                    authorization_id, mode, status, scheduled_at, updated_at, pages_processed
                FROM sync_jobs
                ORDER BY authorization_id, created_at DESC
            )
            SELECT
                a.id,
                a.display_name,
                a.status AS authorization_status,
                a.body_consent_at,
                a.p2p_discovery_status,
                COALESCE(g.group_count, 0) AS group_count,
                COALESCE(g.p2p_count, 0) AS p2p_count,
                COALESCE(g.discovered_conversation_count, 0) AS discovered_conversation_count,
                COALESCE(g.syncable_conversation_count, 0) AS syncable_conversation_count,
                COALESCE(g.archived_conversation_count, 0) AS archived_conversation_count,
                COALESCE(g.backfill_complete_count, 0) AS backfill_complete_count,
                COALESCE(g.syncing_conversation_count, 0) AS syncing_conversation_count,
                COALESCE(g.waiting_conversation_count, 0) AS waiting_conversation_count,
                COALESCE(g.not_readable_conversation_count, 0) AS not_readable_conversation_count,
                COALESCE(g.p2p_backfill_complete_count, 0) AS p2p_backfill_complete_count,
                COALESCE(g.group_backfill_complete_count, 0) AS group_backfill_complete_count,
                g.last_synced_at,
                j.mode AS job_mode,
                j.status AS job_status,
                j.scheduled_at AS job_scheduled_at,
                j.updated_at AS job_updated_at,
                j.pages_processed AS job_pages_processed
            FROM authorizations a
            LEFT JOIN conversation_counts g ON g.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            WHERE a.status <> 'active'
               OR COALESCE(g.backfill_complete_count, 0) < COALESCE(g.syncable_conversation_count, 0)
               OR j.status = 'running'
               OR (j.status = 'queued' AND (j.mode = 'backfill' OR j.scheduled_at <= NOW()))
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
                j.scheduled_at,
                j.updated_at,
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
            "value": int(counts["group_chat_count"] or 0),
            "hint": "同一群被多人看到也只算一个群",
        },
        {
            "label": "去重后的私聊",
            "value": int(counts["p2p_chat_count"] or 0),
            "hint": "仅计入完成新版授权后可读取的 P2P 会话",
        },
        {
            "label": "人员—会话关系",
            "value": int(counts["membership_count"] or 0),
            "hint": "当前可见会话在各授权用户下的关系数",
        },
        {
            "label": "去重后消息索引",
            "value": int(counts["message_count"] or 0),
            "hint": "同一消息只计一次",
        },
        {
            "label": "已加密保存正文",
            "value": int(counts["content_message_count"] or 0),
            "hint": "仅来自完成新版授权后的同步",
        },
        {
            "label": "首次同步未完成会话",
            "value": int(counts["pending_membership_count"] or 0),
            "hint": "不含 Lark 已归档/解散的会话",
        },
        {
            "label": "活动同步任务",
            "value": int(counts["active_job_count"] or 0),
            "hint": "正在运行或已到执行时间；不含下一轮定时检查",
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


def users(page: int = 1) -> dict[str, Any]:
    """Return per-user statistics without double-counting canonical messages."""

    with connection() as conn:
        total_count = int(
            conn.execute("SELECT COUNT(*) AS total_count FROM authorizations").fetchone()["total_count"] or 0
        )
        page, limit, offset = _page_window(page, total_count=total_count)
        rows = conn.execute(
            f"""
            WITH conversation_counts AS (
                {CONVERSATION_COUNTS_SQL}
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
                    authorization_id, mode, status, scheduled_at, updated_at, pages_processed
                FROM sync_jobs
                ORDER BY authorization_id, created_at DESC
            )
            SELECT
                a.id,
                a.display_name,
                a.status AS authorization_status,
                a.authorized_at,
                a.updated_at,
                a.body_consent_at,
                a.p2p_discovery_status,
                r.role AS admin_role,
                COALESCE(g.group_count, 0) AS group_count,
                COALESCE(g.p2p_count, 0) AS p2p_count,
                COALESCE(g.discovered_conversation_count, 0) AS discovered_conversation_count,
                COALESCE(g.syncable_conversation_count, 0) AS syncable_conversation_count,
                COALESCE(g.archived_conversation_count, 0) AS archived_conversation_count,
                COALESCE(g.backfill_complete_count, 0) AS backfill_complete_count,
                COALESCE(g.syncing_conversation_count, 0) AS syncing_conversation_count,
                COALESCE(g.waiting_conversation_count, 0) AS waiting_conversation_count,
                COALESCE(g.not_readable_conversation_count, 0) AS not_readable_conversation_count,
                COALESCE(g.p2p_backfill_complete_count, 0) AS p2p_backfill_complete_count,
                COALESCE(g.group_backfill_complete_count, 0) AS group_backfill_complete_count,
                g.last_synced_at,
                COALESCE(m.message_count, 0) AS message_count,
                COALESCE(m.own_message_count, 0) AS own_message_count,
                j.mode AS job_mode,
                j.status AS job_status,
                j.scheduled_at AS job_scheduled_at,
                j.updated_at AS job_updated_at,
                j.pages_processed AS job_pages_processed
            FROM authorizations a
            LEFT JOIN admin_roles r ON r.authorization_id = a.id AND r.is_active
            LEFT JOIN conversation_counts g ON g.authorization_id = a.id
            LEFT JOIN message_counts m ON m.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            ORDER BY a.authorized_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    return _page_result([_decorate_user(row) for row in rows], page, total_count=total_count)


def user_detail(authorization_id: UUID, conversation_page: int = 1, job_page: int = 1) -> dict[str, Any] | None:
    """Return one user plus their current conversation relationships and jobs."""

    with connection() as conn:
        user = conn.execute(
            f"""
            WITH conversation_counts AS (
                {CONVERSATION_COUNTS_SQL}
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
                    authorization_id, mode, status, scheduled_at, updated_at, pages_processed
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
                a.body_consent_at,
                a.p2p_discovery_status,
                r.role AS admin_role,
                COALESCE(g.group_count, 0) AS group_count,
                COALESCE(g.p2p_count, 0) AS p2p_count,
                COALESCE(g.discovered_conversation_count, 0) AS discovered_conversation_count,
                COALESCE(g.syncable_conversation_count, 0) AS syncable_conversation_count,
                COALESCE(g.archived_conversation_count, 0) AS archived_conversation_count,
                COALESCE(g.backfill_complete_count, 0) AS backfill_complete_count,
                COALESCE(g.syncing_conversation_count, 0) AS syncing_conversation_count,
                COALESCE(g.waiting_conversation_count, 0) AS waiting_conversation_count,
                COALESCE(g.not_readable_conversation_count, 0) AS not_readable_conversation_count,
                COALESCE(g.p2p_backfill_complete_count, 0) AS p2p_backfill_complete_count,
                COALESCE(g.group_backfill_complete_count, 0) AS group_backfill_complete_count,
                g.last_synced_at,
                COALESCE(m.message_count, 0) AS message_count,
                COALESCE(m.own_message_count, 0) AS own_message_count,
                j.mode AS job_mode,
                j.status AS job_status,
                j.scheduled_at AS job_scheduled_at,
                j.updated_at AS job_updated_at,
                j.pages_processed AS job_pages_processed
            FROM authorizations a
            LEFT JOIN admin_roles r ON r.authorization_id = a.id AND r.is_active
            LEFT JOIN conversation_counts g ON g.authorization_id = a.id
            LEFT JOIN message_counts m ON m.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            WHERE a.id = %s
            """,
            (authorization_id, authorization_id, authorization_id),
        ).fetchone()
        if not user:
            return None

        conversation_total_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS total_count
                FROM authorization_chats ac
                LEFT JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.authorization_id = %s
                  AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                """,
                (authorization_id,),
            ).fetchone()["total_count"]
            or 0
        )
        job_total_count = int(
            conn.execute(
                "SELECT COUNT(*) AS total_count FROM sync_jobs WHERE authorization_id = %s",
                (authorization_id,),
            ).fetchone()["total_count"]
            or 0
        )
        conversation_page, conversation_limit, conversation_offset = _page_window(
            conversation_page, DETAIL_PAGE_SIZE, total_count=conversation_total_count
        )
        job_page, job_limit, job_offset = _page_window(job_page, DETAIL_PAGE_SIZE, total_count=job_total_count)

        conversations = conn.execute(
            """
            SELECT
                ac.chat_id,
                c.name,
                c.external,
                c.chat_mode,
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
                c.name, c.external, c.chat_mode
            ORDER BY LOWER(COALESCE(c.name, '')), ac.chat_id
            LIMIT %s OFFSET %s
            """,
            (authorization_id, conversation_limit, conversation_offset),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT
                id, mode, status, pages_processed, chats_discovered, messages_indexed,
                own_messages_indexed, attempts, created_at, started_at, finished_at,
                scheduled_at, updated_at
            FROM sync_jobs
            WHERE authorization_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (authorization_id, job_limit, job_offset),
        ).fetchall()

    return {
        "user": _decorate_user(user),
        "conversations": _page_result(
            [_decorate_conversation_membership(row) for row in conversations],
            conversation_page,
            DETAIL_PAGE_SIZE,
            total_count=conversation_total_count,
        ),
        "jobs": _page_result(
            [_decorate_job(row) for row in jobs], job_page, DETAIL_PAGE_SIZE, total_count=job_total_count
        ),
    }


def chats(page: int = 1) -> dict[str, Any]:
    """Return canonical groups and how many authorised users currently see each."""

    with connection() as conn:
        total_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS total_count
                FROM chats
                WHERE COALESCE(chat_status, 'normal') = 'normal'
                  AND COALESCE(chat_mode, 'group') <> 'p2p'
                """
            ).fetchone()["total_count"]
            or 0
        )
        page, limit, offset = _page_window(page, total_count=total_count)
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
                WHERE ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND COALESCE(c.chat_mode, 'group') <> 'p2p'
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
                  AND COALESCE(c.chat_mode, 'group') <> 'p2p'
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
                c.chat_mode,
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
              AND COALESCE(c.chat_mode, 'group') <> 'p2p'
            ORDER BY LOWER(COALESCE(c.name, '')), c.chat_id
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    return _page_result([_decorate_chat(row) for row in rows], page, total_count=total_count)


def jobs(page: int = 1) -> dict[str, Any]:
    """Return recent worker jobs. Token material is never selected."""

    with connection() as conn:
        total_count = int(conn.execute("SELECT COUNT(*) AS total_count FROM sync_jobs").fetchone()["total_count"] or 0)
        page, limit, offset = _page_window(page, total_count=total_count)
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
                j.scheduled_at,
                j.updated_at,
                a.display_name
            FROM sync_jobs j
            JOIN authorizations a ON a.id = j.authorization_id
            ORDER BY j.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    return _page_result([_decorate_job(row) for row in rows], page, total_count=total_count)


def creators(page: int = 1) -> dict[str, Any]:
    """Return the compact owner-maintained creator identity library."""

    with connection() as conn:
        total_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS total_count
                FROM contact_roles
                WHERE role = 'creator' AND status = 'confirmed'
                """
            ).fetchone()["total_count"]
            or 0
        )
        page, limit, offset = _page_window(page, total_count=total_count)
        rows = conn.execute(
            """
            SELECT
                c.id,
                c.display_name,
                c.created_at,
                c.updated_at,
                COUNT(DISTINCT identifier.id) + COUNT(DISTINCT account.social_account_id) AS identifier_count,
                COUNT(DISTINCT account.social_account_id) AS social_account_count
            FROM contacts c
            JOIN contact_roles role
              ON role.contact_id = c.id AND role.role = 'creator' AND role.status = 'confirmed'
            LEFT JOIN contact_identifiers identifier ON identifier.contact_id = c.id
            LEFT JOIN contact_social_accounts account
              ON account.contact_id = c.id AND account.status = 'confirmed'
            GROUP BY c.id, c.display_name, c.created_at, c.updated_at
            ORDER BY LOWER(c.display_name), c.id
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    return _page_result([_decorate_creator(row) for row in rows], page, total_count=total_count)


def creator_detail(creator_id: UUID, identifier_page: int = 1) -> dict[str, Any] | None:
    with connection() as conn:
        creator = conn.execute(
            """
            SELECT
                c.id,
                c.display_name,
                c.created_at,
                c.updated_at,
                COUNT(DISTINCT identifier.id) + COUNT(DISTINCT account.social_account_id) AS identifier_count,
                COUNT(DISTINCT account.social_account_id) AS social_account_count
            FROM contacts c
            JOIN contact_roles role
              ON role.contact_id = c.id AND role.role = 'creator' AND role.status = 'confirmed'
            LEFT JOIN contact_identifiers identifier ON identifier.contact_id = c.id
            LEFT JOIN contact_social_accounts account
              ON account.contact_id = c.id AND account.status = 'confirmed'
            WHERE c.id = %s
            GROUP BY c.id, c.display_name, c.created_at, c.updated_at
            """,
            (creator_id,),
        ).fetchone()
        if not creator:
            return None
        identifier_total_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS total_count
                FROM (
                    SELECT id
                    FROM contact_identifiers
                    WHERE contact_id = %s
                    UNION ALL
                    SELECT account.id
                    FROM contact_social_accounts relation
                    JOIN social_accounts account ON account.id = relation.social_account_id
                    WHERE relation.contact_id = %s AND relation.status = 'confirmed'
                ) AS identifiers
                """,
                (creator_id, creator_id),
            ).fetchone()["total_count"]
            or 0
        )
        identifier_page, identifier_limit, identifier_offset = _page_window(
            identifier_page, DETAIL_PAGE_SIZE, total_count=identifier_total_count
        )
        identifiers = conn.execute(
            """
            WITH identifiers AS (
                SELECT id, kind, platform, display_value, created_at, updated_at
                FROM contact_identifiers
                WHERE contact_id = %s
                UNION ALL
                SELECT account.id, 'handle' AS kind, account.platform, account.display_handle AS display_value,
                       relation.created_at, relation.updated_at
                FROM contact_social_accounts relation
                JOIN social_accounts account ON account.id = relation.social_account_id
                WHERE relation.contact_id = %s AND relation.status = 'confirmed'
            )
            SELECT id, kind, platform, display_value, created_at, updated_at
            FROM identifiers
            ORDER BY kind, platform, LOWER(display_value), id
            LIMIT %s OFFSET %s
            """,
            (creator_id, creator_id, identifier_limit, identifier_offset),
        ).fetchall()
    return {
        "creator": _decorate_creator(creator),
        "identifiers": _page_result(
            [_decorate_creator_identifier(row) for row in identifiers],
            identifier_page,
            DETAIL_PAGE_SIZE,
            total_count=identifier_total_count,
        ),
    }


def contacts(page: int = 1) -> dict[str, Any]:
    """Return discovered non-internal Lark contacts; no role is inferred here."""

    with connection() as conn:
        total_count = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT contact.id) AS total_count
                FROM contacts contact
                JOIN lark_identities identity ON identity.contact_id = contact.id
                WHERE identity.identity_type = 'user' AND identity.tenant_classification <> 'internal'
                """
            ).fetchone()["total_count"]
            or 0
        )
        page, limit, offset = _page_window(page, total_count=total_count)
        rows = conn.execute(
            """
            SELECT
                contact.id AS contact_id,
                COALESCE(contact.display_name, primary_identity.display_name) AS display_name,
                primary_identity.open_id,
                primary_identity.tenant_key,
                primary_identity.tenant_classification,
                identity_counts.lark_identity_count,
                identity_counts.last_seen_at,
                conversations.conversation_count,
                messages.message_count,
                accounts.account_count
            FROM contacts contact
            JOIN LATERAL (
                SELECT identity.open_id, identity.display_name, identity.tenant_key, identity.tenant_classification
                FROM lark_identities identity
                WHERE identity.contact_id = contact.id
                  AND identity.identity_type = 'user'
                  AND identity.tenant_classification <> 'internal'
                ORDER BY identity.last_seen_at DESC, identity.open_id
                LIMIT 1
            ) AS primary_identity ON TRUE
            JOIN LATERAL (
                SELECT COUNT(*) AS lark_identity_count, MAX(identity.last_seen_at) AS last_seen_at
                FROM lark_identities identity
                WHERE identity.contact_id = contact.id AND identity.identity_type = 'user'
            ) AS identity_counts ON TRUE
            JOIN LATERAL (
                SELECT COUNT(*) AS conversation_count
                FROM (
                    SELECT member.chat_id
                    FROM chat_members member
                    JOIN lark_identities identity ON identity.open_id = member.open_id
                    WHERE identity.contact_id = contact.id
                    UNION
                    SELECT message.chat_id
                    FROM chat_messages message
                    JOIN lark_identities identity ON identity.open_id = message.sender_open_id
                    WHERE identity.contact_id = contact.id
                    UNION
                    SELECT association.chat_id
                    FROM authorization_chats association
                    JOIN lark_identities identity ON identity.open_id = association.p2p_counterpart_open_id
                    WHERE association.is_visible AND identity.contact_id = contact.id
                ) AS contact_conversations
            ) AS conversations ON TRUE
            JOIN LATERAL (
                SELECT COUNT(*) AS message_count
                FROM chat_messages message
                JOIN lark_identities identity ON identity.open_id = message.sender_open_id
                WHERE identity.contact_id = contact.id AND NOT message.is_deleted
            ) AS messages ON TRUE
            JOIN LATERAL (
                SELECT COUNT(DISTINCT observation.social_account_id) AS account_count
                FROM social_account_observations observation
                JOIN lark_identities identity ON identity.open_id = observation.source_open_id
                WHERE identity.contact_id = contact.id
            ) AS accounts ON TRUE
            ORDER BY identity_counts.last_seen_at DESC NULLS LAST,
                     LOWER(COALESCE(contact.display_name, primary_identity.display_name)), contact.id
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    return _page_result([_decorate_contact(row) for row in rows], page, total_count=total_count)


def contact_detail(
    open_id: str,
    conversation_page: int = 1,
    account_page: int = 1,
) -> dict[str, Any] | None:
    """Return one stable Lark contact, its visible conversation facts, and raw account appearances."""

    with connection() as conn:
        contact = conn.execute(
            """
            SELECT
                contact.id AS contact_id,
                COALESCE(contact.display_name, identity.display_name) AS display_name,
                identity.open_id,
                identity.tenant_key,
                identity.tenant_classification,
                contact.created_at,
                contact.updated_at
            FROM lark_identities identity
            JOIN contacts contact ON contact.id = identity.contact_id
            WHERE identity.open_id = %s
              AND identity.identity_type = 'user'
              AND identity.tenant_classification <> 'internal'
            """,
            (open_id,),
        ).fetchone()
        if not contact:
            return None

        contact_id = contact["contact_id"]
        identities = conn.execute(
            """
            SELECT open_id, display_name, tenant_key, tenant_classification, first_seen_at, last_seen_at
            FROM lark_identities
            WHERE contact_id = %s AND identity_type = 'user'
            ORDER BY last_seen_at DESC, open_id
            """,
            (contact_id,),
        ).fetchall()

        conversation_total_count = int(
            conn.execute(
                """
                WITH contact_identities AS (
                    SELECT open_id FROM lark_identities WHERE contact_id = %s
                ), conversation_ids AS (
                    SELECT member.chat_id FROM chat_members member
                    JOIN contact_identities identity USING (open_id)
                    UNION
                    SELECT message.chat_id FROM chat_messages message
                    JOIN contact_identities identity ON identity.open_id = message.sender_open_id
                    UNION
                    SELECT association.chat_id FROM authorization_chats association
                    JOIN contact_identities identity ON identity.open_id = association.p2p_counterpart_open_id
                    WHERE association.is_visible
                )
                SELECT COUNT(*) AS total_count
                FROM conversation_ids ids
                JOIN chats chat ON chat.chat_id = ids.chat_id
                WHERE COALESCE(chat.chat_status, 'normal') = 'normal'
                """,
                (contact_id,),
            ).fetchone()["total_count"]
            or 0
        )
        conversation_page, conversation_limit, conversation_offset = _page_window(
            conversation_page, DETAIL_PAGE_SIZE, total_count=conversation_total_count
        )
        conversations = conn.execute(
            """
            WITH contact_identities AS (
                SELECT open_id FROM lark_identities WHERE contact_id = %s
            ), conversation_ids AS (
                SELECT member.chat_id FROM chat_members member
                JOIN contact_identities identity USING (open_id)
                UNION
                SELECT message.chat_id FROM chat_messages message
                JOIN contact_identities identity ON identity.open_id = message.sender_open_id
                UNION
                SELECT association.chat_id FROM authorization_chats association
                JOIN contact_identities identity ON identity.open_id = association.p2p_counterpart_open_id
                WHERE association.is_visible
            )
            SELECT
                chat.chat_id,
                chat.name,
                COALESCE(chat.chat_mode, 'group') AS chat_mode,
                chat.external,
                member_counts.member_count,
                member_counts.member_count_status,
                message_counts.message_count,
                message_counts.contact_message_count,
                message_counts.last_message_at
            FROM conversation_ids ids
            JOIN chats chat ON chat.chat_id = ids.chat_id
            LEFT JOIN LATERAL (
                SELECT access.member_count, access.member_count_status
                FROM authorization_chats access
                WHERE access.chat_id = chat.chat_id
                  AND access.is_visible
                  AND access.sync_enabled
                ORDER BY
                    CASE WHEN access.member_count_status = 'succeeded' THEN 0 ELSE 1 END,
                    access.member_count_checked_at DESC NULLS LAST,
                    access.last_seen_at DESC
                LIMIT 1
            ) AS member_counts ON TRUE
            JOIN LATERAL (
                SELECT
                    COUNT(*) FILTER (WHERE NOT message.is_deleted) AS message_count,
                    COUNT(*) FILTER (
                        WHERE NOT message.is_deleted
                          AND message.sender_open_id IN (SELECT open_id FROM contact_identities)
                    ) AS contact_message_count,
                    MAX(message.message_create_time) FILTER (WHERE NOT message.is_deleted) AS last_message_at
                FROM chat_messages message
                WHERE message.chat_id = chat.chat_id
            ) AS message_counts ON TRUE
            WHERE COALESCE(chat.chat_status, 'normal') = 'normal'
            ORDER BY message_counts.last_message_at DESC NULLS LAST, LOWER(COALESCE(chat.name, '')), chat.chat_id
            LIMIT %s OFFSET %s
            """,
            (contact_id, conversation_limit, conversation_offset),
        ).fetchall()

        account_total_count = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT observation.social_account_id) AS total_count
                FROM social_account_observations observation
                JOIN lark_identities identity ON identity.open_id = observation.source_open_id
                WHERE identity.contact_id = %s
                """,
                (contact_id,),
            ).fetchone()["total_count"]
            or 0
        )
        account_page, account_limit, account_offset = _page_window(
            account_page, DETAIL_PAGE_SIZE, total_count=account_total_count
        )
        accounts = conn.execute(
            """
            SELECT
                account.id,
                account.platform,
                account.display_handle,
                COUNT(observation.id) AS observation_count,
                MAX(observation.last_observed_at) AS last_observed_at
            FROM social_account_observations observation
            JOIN lark_identities identity ON identity.open_id = observation.source_open_id
            JOIN social_accounts account ON account.id = observation.social_account_id
            WHERE identity.contact_id = %s
            GROUP BY account.id, account.platform, account.display_handle
            ORDER BY MAX(observation.last_observed_at) DESC, account.platform, account.normalized_handle
            LIMIT %s OFFSET %s
            """,
            (contact_id, account_limit, account_offset),
        ).fetchall()

    return {
        "contact": _decorate_contact(contact),
        "identities": [_decorate_lark_identity(row) for row in identities],
        "conversations": _page_result(
            [_decorate_contact_conversation(row) for row in conversations],
            conversation_page,
            DETAIL_PAGE_SIZE,
            total_count=conversation_total_count,
        ),
        "accounts": _page_result(
            [_decorate_social_account(row) for row in accounts],
            account_page,
            DETAIL_PAGE_SIZE,
            total_count=account_total_count,
        ),
    }


def accounts(page: int = 1) -> dict[str, Any]:
    """Return independent social accounts and their raw message appearances."""

    with connection() as conn:
        total_count = int(
            conn.execute("SELECT COUNT(*) AS total_count FROM social_accounts").fetchone()["total_count"] or 0
        )
        page, limit, offset = _page_window(page, total_count=total_count)
        rows = conn.execute(
            """
            SELECT
                account.id,
                account.platform,
                account.display_handle,
                account.created_at,
                account.updated_at,
                owner.display_name AS owner_display_name,
                observations.observation_count,
                observations.source_count,
                observations.last_observed_at
            FROM social_accounts account
            LEFT JOIN contact_social_accounts owner_relation
              ON owner_relation.social_account_id = account.id
             AND owner_relation.relationship = 'owner'
             AND owner_relation.status = 'confirmed'
            LEFT JOIN contacts owner ON owner.id = owner_relation.contact_id
            JOIN LATERAL (
                SELECT
                    COUNT(*) AS observation_count,
                    COUNT(DISTINCT source_open_id) AS source_count,
                    MAX(last_observed_at) AS last_observed_at
                FROM social_account_observations observation
                WHERE observation.social_account_id = account.id
            ) AS observations ON TRUE
            ORDER BY observations.last_observed_at DESC NULLS LAST, account.platform, account.normalized_handle
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    return _page_result([_decorate_social_account(row) for row in rows], page, total_count=total_count)


def account_detail(account_id: UUID, evidence_page: int = 1) -> dict[str, Any] | None:
    """Return a social account with only its authorized, auditable message evidence."""

    with connection() as conn:
        account = conn.execute(
            """
            SELECT
                account.id,
                account.platform,
                account.display_handle,
                account.created_at,
                account.updated_at,
                owner.id AS owner_contact_id,
                owner.display_name AS owner_display_name,
                observations.observation_count,
                observations.source_count,
                observations.last_observed_at
            FROM social_accounts account
            LEFT JOIN contact_social_accounts owner_relation
              ON owner_relation.social_account_id = account.id
             AND owner_relation.relationship = 'owner'
             AND owner_relation.status = 'confirmed'
            LEFT JOIN contacts owner ON owner.id = owner_relation.contact_id
            JOIN LATERAL (
                SELECT
                    COUNT(*) AS observation_count,
                    COUNT(DISTINCT source_open_id) AS source_count,
                    MAX(last_observed_at) AS last_observed_at
                FROM social_account_observations observation
                WHERE observation.social_account_id = account.id
            ) AS observations ON TRUE
            WHERE account.id = %s
            """,
            (account_id,),
        ).fetchone()
        if not account:
            return None
        evidence_total_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS total_count
                FROM social_account_observations
                WHERE social_account_id = %s
                """,
                (account_id,),
            ).fetchone()["total_count"]
            or 0
        )
        evidence_page, evidence_limit, evidence_offset = _page_window(
            evidence_page, DETAIL_PAGE_SIZE, total_count=evidence_total_count
        )
        evidence = conn.execute(
            """
            SELECT
                observation.id,
                observation.extractor,
                observation.created_at,
                observation.last_observed_at,
                observation.chat_id,
                observation.message_id,
                chat.name AS chat_name,
                COALESCE(chat.chat_mode, 'group') AS chat_mode,
                source.open_id AS source_open_id,
                source.contact_id AS source_contact_id,
                source.display_name AS source_display_name,
                source.tenant_classification AS source_tenant_classification,
                authorization.display_name AS observed_by_display_name,
                message.message_type,
                message.message_create_time,
                message.is_deleted,
                CASE
                    WHEN authorization.status = 'active'
                     AND authorization_chat.is_visible
                     AND authorization.body_consent_at IS NOT NULL
                     AND scope.observed_at >= authorization.body_consent_at
                     AND NOT message.is_deleted
                    THEN message.body_ciphertext
                END AS body_ciphertext
            FROM social_account_observations observation
            JOIN chat_messages message
              ON message.chat_id = observation.chat_id AND message.message_id = observation.message_id
            JOIN chats chat ON chat.chat_id = observation.chat_id
            JOIN authorizations authorization ON authorization.id = observation.observed_by_authorization_id
            LEFT JOIN authorization_chats authorization_chat
              ON authorization_chat.authorization_id = observation.observed_by_authorization_id
             AND authorization_chat.chat_id = observation.chat_id
            LEFT JOIN authorization_message_observations scope
              ON scope.authorization_id = observation.observed_by_authorization_id
             AND scope.chat_id = observation.chat_id
             AND scope.message_id = observation.message_id
            LEFT JOIN lark_identities source ON source.open_id = observation.source_open_id
            WHERE observation.social_account_id = %s
            ORDER BY observation.last_observed_at DESC, observation.id DESC
            LIMIT %s OFFSET %s
            """,
            (account_id, evidence_limit, evidence_offset),
        ).fetchall()

    cipher = message_body_cipher(get_settings().token_encryption_key)
    return {
        "account": _decorate_social_account(account),
        "evidence": _page_result(
            [_decorate_account_observation(row, cipher) for row in evidence],
            evidence_page,
            DETAIL_PAGE_SIZE,
            total_count=evidence_total_count,
        ),
    }


def normalize_message_conversation_query(value: str | None) -> str:
    """Keep the owner-side conversation search bounded and predictable."""

    return (value or "").strip()[:MAX_MESSAGE_CONVERSATION_QUERY_LENGTH]


def normalize_message_conversation_type(value: str | None) -> str:
    """Return a supported message-directory type filter."""

    return value if value in MESSAGE_CONVERSATION_TYPES else "all"


def message_conversations(
    authorization_id: UUID,
    page: int = 1,
    query: str = "",
    chat_type: str = "all",
) -> dict[str, Any]:
    """Return one filtered, five-conversation page for the owner-only content view."""

    query = normalize_message_conversation_query(query)
    chat_type = normalize_message_conversation_type(chat_type)
    directory_params = (authorization_id, query, query, query, chat_type, chat_type, chat_type)
    with connection() as conn:
        total_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*) AS total_count
                FROM (
                    {MESSAGE_CONVERSATION_DIRECTORY_SQL}
                    {MESSAGE_CONVERSATION_FILTER_SQL}
                ) AS filtered_conversations
                """,
                directory_params,
            ).fetchone()["total_count"]
            or 0
        )
        page, limit, offset = _page_window(page, MESSAGE_CONVERSATION_PAGE_SIZE, total_count=total_count)
        rows = conn.execute(
            f"""
            WITH conversations AS (
                {MESSAGE_CONVERSATION_DIRECTORY_SQL}
                {MESSAGE_CONVERSATION_FILTER_SQL}
                ORDER BY LOWER(COALESCE(c.name, counterpart.display_name, '')), ac.chat_id
                LIMIT %s OFFSET %s
            )
            {MESSAGE_CONVERSATION_COUNTS_SQL}
            ORDER BY LOWER(COALESCE(name, counterpart_display_name, '')), chat_id
            """,
            (*directory_params, limit, offset),
        ).fetchall()
    return _page_result(
        [_decorate_message_conversation(row) for row in rows],
        page,
        MESSAGE_CONVERSATION_PAGE_SIZE,
        total_count=total_count,
    )


def message_conversation(authorization_id: UUID, chat_id: str) -> dict[str, Any] | None:
    """Verify one visible conversation without loading the user's full directory."""

    with connection() as conn:
        row = conn.execute(
            f"""
            WITH conversations AS (
                {MESSAGE_CONVERSATION_DIRECTORY_SQL}
                  AND ac.chat_id = %s
            )
            {MESSAGE_CONVERSATION_COUNTS_SQL}
            """,
            (authorization_id, chat_id),
        ).fetchone()
    return _decorate_message_conversation(row) if row else None


def message_page(
    authorization_id: UUID,
    chat_id: str,
    page: int,
) -> dict[str, Any]:
    """Return one explicit conversation's readable, consented message content page."""

    filters = (authorization_id, chat_id)
    with connection() as conn:
        metrics = conn.execute(
            """
            WITH visible_messages AS (
                SELECT m.*, o.sent_by_author, c.name, COALESCE(c.chat_mode, 'group') AS chat_mode
                FROM authorizations a
                JOIN authorization_chats ac ON ac.authorization_id = a.id
                JOIN chats c ON c.chat_id = ac.chat_id
                JOIN authorization_message_observations o
                  ON o.authorization_id = ac.authorization_id AND o.chat_id = ac.chat_id
                JOIN chat_messages m ON m.chat_id = o.chat_id AND m.message_id = o.message_id
                WHERE a.id = %s
                  AND a.body_consent_at IS NOT NULL
                  AND o.observed_at >= a.body_consent_at
                  AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND NOT m.is_deleted
                  AND m.chat_id = %s
            )
            SELECT
                COUNT(*) AS message_count,
                COUNT(*) FILTER (WHERE body_ciphertext IS NOT NULL) AS content_count,
                COUNT(*) FILTER (WHERE sent_by_author) AS own_message_count,
                COUNT(*) FILTER (WHERE chat_mode = 'p2p') AS p2p_message_count
            FROM visible_messages
            """,
            filters,
        ).fetchone()
        total_count = int(metrics["content_count"] or 0)
        page, limit, offset = _page_window(page, MESSAGE_PAGE_SIZE, total_count=total_count)
        rows = conn.execute(
            """
            WITH visible_messages AS (
                SELECT
                    m.*, o.sent_by_author, c.name, COALESCE(c.chat_mode, 'group') AS chat_mode,
                    identity.display_name AS sender_display_name
                FROM authorizations a
                JOIN authorization_chats ac ON ac.authorization_id = a.id
                JOIN chats c ON c.chat_id = ac.chat_id
                JOIN authorization_message_observations o
                  ON o.authorization_id = ac.authorization_id AND o.chat_id = ac.chat_id
                JOIN chat_messages m ON m.chat_id = o.chat_id AND m.message_id = o.message_id
                LEFT JOIN lark_identities identity ON identity.open_id = m.sender_open_id
                WHERE a.id = %s
                  AND a.body_consent_at IS NOT NULL
                  AND o.observed_at >= a.body_consent_at
                  AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND NOT m.is_deleted
                  AND m.chat_id = %s
            )
            SELECT *
            FROM visible_messages
            WHERE body_ciphertext IS NOT NULL
            ORDER BY message_create_time DESC NULLS LAST, message_id DESC
            LIMIT %s OFFSET %s
            """,
            (*filters, limit, offset),
        ).fetchall()

    cipher = message_body_cipher(get_settings().token_encryption_key)
    items = [_decorate_message(row, cipher) for row in rows[:MESSAGE_PAGE_SIZE]]
    return {
        "metrics": {key: int(metrics[key] or 0) for key in metrics},
        **_page_result(items, page, MESSAGE_PAGE_SIZE, total_count=total_count),
    }


def record_message_view(viewer_id: UUID, target_authorization_id: UUID, chat_id: str | None) -> None:
    """Audit an owner content-page view without ever placing message text in the log."""

    metadata = {"target_authorization_id": str(target_authorization_id)}
    if chat_id:
        metadata["chat_id"] = chat_id
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO audit_events (id, authorization_id, action, metadata)
            VALUES (%s, %s, 'admin_message_content_viewed', %s::jsonb)
            """,
            (uuid4(), viewer_id, json.dumps(metadata)),
        )


def record_account_view(viewer_id: UUID, account_id: UUID) -> None:
    """Audit a raw account-evidence view without putting message text in the log."""

    with connection() as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO audit_events (id, authorization_id, action, metadata)
            VALUES (%s, %s, 'admin_social_account_viewed', %s::jsonb)
            """,
            (uuid4(), viewer_id, json.dumps({"social_account_id": str(account_id)})),
        )


def _page_window(page: int, page_size: int = LIST_PAGE_SIZE, *, total_count: int | None = None) -> tuple[int, int, int]:
    page = max(1, min(page, MAX_PAGE))
    if total_count is not None:
        page = min(page, max(1, _total_pages(total_count, page_size)))
    return page, page_size + 1, (page - 1) * page_size


def _page_result(
    items: list[dict[str, Any]],
    page: int,
    page_size: int = LIST_PAGE_SIZE,
    *,
    total_count: int | None = None,
) -> dict[str, Any]:
    if total_count is None:
        total_count = (page - 1) * page_size + len(items)
    total_count = max(0, int(total_count))
    total_pages = _total_pages(total_count, page_size)
    return {
        "items": items[:page_size],
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_next": page < total_pages,
    }


def _total_pages(total_count: int, page_size: int) -> int:
    return (max(0, total_count) + page_size - 1) // page_size


def _decorate_user(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "未命名用户"
    item["message_count_label"] = _message_count_label(item.get("message_count"))
    for field in ("authorized_at", "updated_at", "last_synced_at", "job_scheduled_at", "job_updated_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    item["authorization_status_label"], item["authorization_status_badge"] = _authorization_status(
        item.get("authorization_status")
    )
    item["job_status_label"], item["job_status_badge"] = _job_status(
        item.get("job_status"), item.get("job_mode"), item.get("job_scheduled_at")
    )
    item["content_access_label"] = "已完成新版授权" if item.get("body_consent_at") else "需新版授权"
    item["content_access_badge"] = "success" if item.get("body_consent_at") else "warning"
    item["p2p_status_label"], item["p2p_status_badge"] = _p2p_status(item.get("p2p_discovery_status"))
    item["sync_state_label"], item["sync_state_badge"], item["sync_state_detail"] = sync_summary(item)
    item["sync_progress_label"] = _sync_progress_label(item)
    item["p2p_content_progress_label"] = _mode_progress_label(item, "p2p")
    item["group_content_progress_label"] = _mode_progress_label(item, "group")
    return item


def _decorate_conversation_membership(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_mode_label"] = _chat_mode_label(item["chat_mode"])
    item["name"] = _conversation_name(item.get("name"), item["chat_mode"], item.get("chat_id"))
    item["member_count_label"] = (
        "—" if item["chat_mode"] == "p2p" else _member_count(item.get("member_count"), item.get("member_count_status"))
    )
    item["sync_status_label"], item["sync_status_badge"] = _sync_status(item.get("sync_status"))
    item["last_synced_at_label"] = _format_time(item.get("last_incremental_at") or item.get("last_backfill_at"))
    item["message_count_label"] = _message_count_label(item.get("message_count"))
    return item


def _decorate_chat(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["name"] = item.get("name") or "未命名群聊"
    item["chat_mode_label"] = _chat_mode_label(item.get("chat_mode"))
    item["member_count_label"] = _member_count(item.get("member_count"), item.get("member_count_status"))
    item["message_count_label"] = _message_count_label(item.get("message_count"))
    for field in ("last_seen_at", "last_synced_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    return item


def _decorate_message_conversation(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_mode_label"] = _chat_mode_label(item["chat_mode"])
    name = item.get("name") or item.get("counterpart_display_name")
    item["name"] = _conversation_name(name, item["chat_mode"], item.get("chat_id"))
    item["member_count_label"] = (
        "—" if item["chat_mode"] == "p2p" else _member_count(item.get("member_count"), item.get("member_count_status"))
    )
    item["message_count_label"] = _message_count_label(item.get("message_count"))
    return item


def _decorate_message(row: dict[str, Any], cipher: TokenCipher) -> dict[str, Any]:
    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_mode_label"] = _chat_mode_label(item["chat_mode"])
    item["chat_name"] = _conversation_name(item.get("name"), item["chat_mode"], item.get("chat_id"))
    item["sender_label"] = (
        "本人" if item.get("sent_by_author") else item.get("sender_display_name") or "会话对方 / 群成员"
    )
    item["created_at_label"] = _message_time(item.get("message_create_time"))
    try:
        body = cipher.decrypt(item["body_ciphertext"])
    except (InvalidTag, UnicodeDecodeError, ValueError):
        item["content"] = "（正文无法读取）"
    else:
        item["content"] = _readable_body(body, item.get("message_type"))
    return item


def _readable_body(raw: str, message_type: str | None) -> str:
    """Show text and rich-text content without leaking raw JSON into the UI."""

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    texts = _body_text_values(parsed)
    if texts:
        return "\n".join(texts)
    kind = message_type or "未知类型"
    return f"（{kind} 消息没有可展示的文本）"


def _conversation_name(name: Any, chat_mode: str, chat_id: Any) -> str:
    if isinstance(name, str) and name:
        return name
    if chat_mode == "p2p":
        return f"未命名私聊 · {str(chat_id)[-8:]}"
    return "未命名群聊"


def _body_text_values(value: Any) -> list[str]:
    texts: list[str] = []

    def collect(current: Any) -> None:
        if isinstance(current, dict):
            text = current.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
            for key, child in current.items():
                if key != "text":
                    collect(child)
        elif isinstance(current, list):
            for child in current:
                collect(child)

    collect(value)
    return texts


def _decorate_job(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "未命名用户"
    for field in ("created_at", "started_at", "finished_at", "scheduled_at", "updated_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    item["mode_label"] = "首次回填" if item.get("mode") == "backfill" else "定时更新"
    item["status_label"], item["status_badge"] = _job_status(
        item.get("status"), item.get("mode"), item.get("scheduled_at")
    )
    return item


def _decorate_creator(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "未命名达人"
    item["identifier_count"] = int(item.get("identifier_count") or 0)
    item["social_account_count"] = int(item.get("social_account_count") or 0)
    for field in ("created_at", "updated_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    return item


def _decorate_creator_identifier(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["kind_label"] = {
        "handle": "平台账号",
        "email": "邮箱",
        "phone": "电话",
        "crm_id": "CRM ID",
        "alias": "别名",
    }.get(item.get("kind"), "身份标识")
    item["platform_label"] = item.get("platform") or "—"
    item["created_at_label"] = _format_time(item.get("created_at"))
    return item


def _decorate_contact(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "Lark 参与者（姓名未返回）"
    item["tenant_classification_label"], item["tenant_classification_badge"] = _tenant_classification(
        item.get("tenant_classification")
    )
    for field in ("lark_identity_count", "conversation_count", "message_count", "account_count"):
        item[field] = int(item.get(field) or 0)
    for field in ("created_at", "updated_at", "last_seen_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    return item


def _decorate_lark_identity(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "姓名未返回"
    item["tenant_classification_label"], item["tenant_classification_badge"] = _tenant_classification(
        item.get("tenant_classification")
    )
    for field in ("first_seen_at", "last_seen_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    return item


def _decorate_contact_conversation(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_mode_label"] = _chat_mode_label(item["chat_mode"])
    item["name"] = _conversation_name(item.get("name"), item["chat_mode"], item.get("chat_id"))
    item["member_count_label"] = (
        "—" if item["chat_mode"] == "p2p" else _member_count(item.get("member_count"), item.get("member_count_status"))
    )
    item["message_count"] = int(item.get("message_count") or 0)
    item["contact_message_count"] = int(item.get("contact_message_count") or 0)
    item["last_message_at_label"] = _format_time(item.get("last_message_at"))
    return item


def _decorate_social_account(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["identifier_label"] = " · ".join(
        value for value in (item.get("platform"), item.get("display_handle")) if value
    ) or "未命名账号"
    item["owner_label"] = item.get("owner_display_name") or "尚未人工确认"
    for field in ("observation_count", "source_count"):
        item[field] = int(item.get(field) or 0)
    for field in ("created_at", "updated_at", "last_observed_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    return item


def _decorate_account_observation(row: dict[str, Any], cipher: TokenCipher) -> dict[str, Any]:
    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_name"] = _conversation_name(item.get("chat_name"), item["chat_mode"], item.get("chat_id"))
    item["source_label"] = item.get("source_display_name") or "Lark 参与者（姓名未返回）"
    item["source_tenant_label"], item["source_tenant_badge"] = _tenant_classification(
        item.get("source_tenant_classification")
    )
    item["extractor_label"] = item.get("extractor") or "消息规则"
    item["created_at_label"] = _message_time(item.get("message_create_time") or item.get("created_at"))
    item["last_observed_at_label"] = _format_time(item.get("last_observed_at"))
    if item.get("is_deleted"):
        item["content"] = "（该消息已删除）"
    elif not item.get("body_ciphertext"):
        item["content"] = "（消息正文不在当前可读取范围）"
    else:
        try:
            body = cipher.decrypt(item["body_ciphertext"])
        except (InvalidTag, UnicodeDecodeError, ValueError):
            item["content"] = "（正文无法读取）"
        else:
            item["content"] = _readable_body(body, item.get("message_type"))
    return item


def _format_time(value: datetime | None) -> str:
    if not value:
        return "尚未完成"
    return value.astimezone(LONDON_TIME_ZONE).strftime("%Y-%m-%d %H:%M %Z")


def _message_time(value: datetime | None) -> str:
    if not value:
        return "时间未知"
    return value.astimezone(LONDON_TIME_ZONE).strftime("%Y-%m-%d %H:%M %Z")


def sync_summary(facts: dict[str, Any], *, now: datetime | None = None) -> tuple[str, str, str]:
    """Collapse queue, discovery, and per-conversation facts into one user-facing state."""

    if facts.get("authorization_status") != "active":
        return "需要重新授权", "warning", "Lark 授权状态需要处理后才能继续同步。"

    job_status = facts.get("job_status")
    if job_status == "attention_required":
        return "需要重新授权", "warning", "Lark 要求重新确认授权；现有数据保持不变。"
    if job_status == "failed":
        return "同步需要处理", "danger", "最近一次同步未完成；请查看同步任务中的安全错误摘要。"

    total = int(facts.get("syncable_conversation_count") or 0)
    completed = int(facts.get("backfill_complete_count") or 0)
    syncing = int(facts.get("syncing_conversation_count") or 0)
    waiting = int(facts.get("waiting_conversation_count") or 0)
    unreadable = int(facts.get("not_readable_conversation_count") or 0)
    archived = int(facts.get("archived_conversation_count") or 0)
    job_mode = facts.get("job_mode")
    scheduled_at = facts.get("job_scheduled_at")
    current = now or datetime.now(UTC)

    if total == 0:
        if job_status in {"queued", "running"}:
            return "正在发现会话", "primary", "正在从 Lark 建立可同步会话目录。"
        return "暂无可同步会话", "secondary", "Lark 尚未返回可同步的群聊或私聊。"

    if completed < total:
        label = "首次同步中" if job_status == "running" else "等待首次同步"
        parts = [f"已完成 {completed}/{total} 个可同步会话"]
        if syncing:
            parts.append(f"{syncing} 个正在读取")
        if waiting:
            parts.append(f"{waiting} 个等待处理")
        if unreadable:
            parts.append(f"{unreadable} 个 Lark 未允许读取")
        if archived:
            parts.append(f"另有 {archived} 个 Lark 已归档/解散会话不计划回填")
        return label, "primary", "；".join(parts) + "。"

    if unreadable:
        detail = f"首次同步已完成；{unreadable} 个会话未获 Lark 读取许可。"
        if archived:
            detail += f"另有 {archived} 个已归档/解散会话不计划回填。"
        return "已接入（部分不可读）", "warning", detail

    if job_mode == "incremental" and job_status == "running":
        return "定时更新中", "primary", "首次同步已完成，正在执行本轮增量检查。"
    if job_mode == "incremental" and job_status == "queued":
        if isinstance(scheduled_at, datetime) and scheduled_at > current:
            return "已接入 · 定时同步", "success", f"首次同步已完成；下一轮检查将在 {_format_time(scheduled_at)}。"
        return "准备定时更新", "primary", "首次同步已完成，worker 正在等待本轮增量检查执行。"

    detail = "首次同步已完成，系统会每 5 分钟检查一次新增消息。"
    if archived:
        detail += f"另有 {archived} 个 Lark 已归档/解散会话不计划回填。"
    return "已接入 · 定时同步", "success", detail


def _sync_progress_label(facts: dict[str, Any]) -> str:
    total = int(facts.get("syncable_conversation_count") or 0)
    completed = int(facts.get("backfill_complete_count") or 0)
    return f"{completed} / {total}" if total else "—"


def _mode_progress_label(facts: dict[str, Any], mode: str) -> str:
    total_field = "p2p_count" if mode == "p2p" else "group_count"
    completed_field = "p2p_backfill_complete_count" if mode == "p2p" else "group_backfill_complete_count"
    return f"{int(facts.get(completed_field) or 0)} / {int(facts.get(total_field) or 0)}"


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


def _job_status(
    value: str | None,
    mode: str | None = None,
    scheduled_at: datetime | None = None,
) -> tuple[str, str]:
    if value == "queued" and mode == "incremental" and scheduled_at and scheduled_at > datetime.now(UTC):
        return "等待下一轮检查", "secondary"
    mapping = {
        "queued": ("等待执行", "secondary"),
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


def _p2p_status(value: str | None) -> tuple[str, str]:
    mapping = {
        "pending": ("等待私聊目录", "secondary"),
        "succeeded": ("私聊目录已发现", "success"),
        "unavailable": ("私聊目录未返回", "warning"),
        "attention_required": ("需要重新授权", "warning"),
    }
    return mapping.get(value or "", ("尚未检查", "secondary"))


def _chat_mode_label(value: str | None) -> str:
    if value == "p2p":
        return "私聊"
    if value == "topic":
        return "专题群"
    return "群聊"


def _tenant_classification(value: str | None) -> tuple[str, str]:
    mapping = {
        "internal": ("公司成员", "primary"),
        "known_external": ("外部成员", "success"),
        "unresolved": ("租户未返回", "secondary"),
    }
    return mapping.get(value or "", ("租户状态未知", "secondary"))


def _member_count(count: int | None, status: str | None) -> str:
    if count is not None:
        return str(count)
    if status == "pending":
        return "正在获取"
    return "Lark 未返回"


def _message_count_label(count: int | None) -> str:
    return f"{int(count or 0)} 条消息" if count else "尚无消息"

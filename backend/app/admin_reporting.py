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

from . import contact_handles
from .admin_permissions import PAGE_KEYS
from .config import get_settings
from .contact_taxonomy import CONTACT_ROLE_CODES, ORGANIZATION_KIND_LABELS, ROLE_LABELS, role_label
from .crypto import TokenCipher, message_body_cipher
from .db import connection
from .evidence_access import BODY_READ_SCOPE_SQL
from .identity_profiles import safe_avatar_url
from .lark_api import private_chat_url
from .message_mentions import mention_names
from .message_text import display_body as _readable_body

CONTACT_IDENTITY_POLICY = "people-identity-v1"  # Historical classification provenance, read-only.
LONDON_TIME_ZONE = ZoneInfo("Europe/London")
LIST_PAGE_SIZE = 25
DETAIL_PAGE_SIZE = 10
MAX_PAGE = 10_000
MESSAGE_PAGE_SIZE = 100
MESSAGE_CONVERSATION_PAGE_SIZE = 20
MAX_MESSAGE_CONVERSATION_QUERY_LENGTH = 120
MAX_ORGANIZATION_QUERY_LENGTH = 120
MESSAGE_CONVERSATION_TYPES = frozenset({"all", "group", "p2p"})
CONTACT_CATEGORIES = frozenset({"all", "unclassified", *CONTACT_ROLE_CODES})
GROUP_PURPOSE_BADGES = {
    "support": "azure", "community": "indigo", "training": "purple",
    "category": "cyan", "campaign": "orange", "unknown": "secondary",
}
ORGANIZATION_STATUSES = frozenset({"all", "verified", "candidate", "unresolved"})
MESSAGE_CONVERSATION_DIRECTORY_SQL = f"""
    SELECT
        ac.authorization_id,
        ac.chat_id,
        c.name,
        c.external,
        COALESCE(c.chat_mode, 'group') AS chat_mode,
        counterpart.display_name AS counterpart_display_name,
        ac.member_count,
        ac.member_count_status,
        ac.sync_generation,
        a.body_consent_at
    FROM authorizations a
    JOIN authorization_chats ac ON ac.authorization_id = a.id
    JOIN chats c ON c.chat_id = ac.chat_id
    LEFT JOIN lark_identities counterpart ON counterpart.open_id = ac.p2p_counterpart_open_id
    WHERE a.id = %s
      AND a.status = 'active' AND a.body_consent_at IS NOT NULL
      AND {BODY_READ_SCOPE_SQL}
      AND ac.is_visible AND ac.sync_enabled
      AND ac.sync_status IS DISTINCT FROM 'not_readable'
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
                    authorization_id, mode, status, scheduled_at, updated_at, pages_processed,
                    chats_discovered, discovery_status, last_progress_at, last_http_status, last_error_code
                FROM sync_jobs
                ORDER BY authorization_id, created_at DESC
            )
            SELECT
                a.id,
                a.display_name,
                a.status AS authorization_status,
                a.body_consent_at,
                a.scopes,
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
                j.pages_processed AS job_pages_processed,
                j.chats_discovered AS job_chats_discovered,
                j.discovery_status AS job_discovery_status,
                j.last_progress_at AS job_last_progress_at,
                j.last_http_status AS job_last_http_status,
                j.last_error_code AS job_last_error_code
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
                j.discovery_status,
                j.last_progress_at,
                j.last_http_status,
                j.last_error_code,
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
        admission = conn.execute(
            """
            SELECT state.paused_until, state.last_rate_limited_at, state.rate_limited_count,
                   (SELECT COUNT(*) FROM lark_sync_request_leases WHERE expires_at > NOW()) AS in_flight
            FROM lark_sync_admission_state state
            WHERE state.id
            """
        ).fetchone()

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
        "lark_admission": _decorate_lark_admission(admission),
    }


def runtime_settings() -> tuple[dict[str, str], ...]:
    """Expose operating values useful to an owner without exposing secrets."""

    settings = get_settings()
    history = "全部 API 可返回历史" if settings.sync_lookback_days == 0 else f"最近 {settings.sync_lookback_days} 天"
    return (
        {"label": "首次回填范围", "value": history},
        {"label": "增量同步周期", "value": _seconds_label(settings.sync_interval_seconds)},
        {"label": "增量重叠窗口", "value": _seconds_label(settings.sync_overlap_seconds)},
        {"label": "成员数刷新周期", "value": _seconds_label(settings.member_count_refresh_seconds)},
        {"label": "Lark 全局并发", "value": f"{settings.lark_sync_max_inflight} 个请求"},
        {
            "label": "单人会话并发上限",
            "value": f"{settings.lark_sync_per_authorization_max_inflight} 个（仅一人积压时可使用全部槽位）",
        },
        {"label": "Lark 全局请求速率", "value": f"{settings.lark_sync_requests_per_second} 请求 / 秒"},
        {"label": "成员目录优先级", "value": "消息回填空闲后，以 1 个低优先级槽位刷新"},
    )


def normalize_user_permission_filter(value: str) -> str:
    return value if value in {"all", "none", "any", "owner"} | PAGE_KEYS else "all"


def message_users(page: int = 1, *, source_id: UUID | None = None,
                  source_ids: list[UUID] | None = None) -> dict[str, Any]:
    """Source picker metadata only: never aggregate the message ledger."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, display_name, status AS authorization_status, body_consent_at, scopes
               FROM authorizations WHERE (%s::uuid IS NULL OR id=%s)
               AND (%s::uuid[] IS NULL OR id=ANY(%s::uuid[]))
               ORDER BY authorized_at DESC""", (source_id, source_id, source_ids, source_ids),
        ).fetchall()
    if source_ids is not None:
        rows.sort(key=lambda row: row.get("display_name") or "")
        return _page_result([_decorate_user(row) for row in rows], 1, total_count=len(rows))
    page, _, offset = _page_window(page, total_count=len(rows))
    return _page_result([_decorate_user(row) for row in rows[offset:offset + LIST_PAGE_SIZE]],
                        page, total_count=len(rows))


def message_user(source_id: UUID) -> dict[str, Any] | None:
    rows = message_users(source_id=source_id)["items"]
    return {"user": rows[0]} if rows else None


def users(page: int = 1, query: str = "", permission_filter: str = "all") -> dict[str, Any]:
    """Return per-user statistics without double-counting canonical messages."""

    query = normalize_message_conversation_query(query)
    permission_filter = normalize_user_permission_filter(permission_filter)
    owner_account = "COALESCE(r.role = 'owner', FALSE)"
    owner = f"({owner_account} AND COALESCE(r.is_active, FALSE))"
    backend_access = f"""(a.status = 'active' AND ({owner} OR (NOT {owner_account} AND EXISTS (
        SELECT 1 FROM admin_page_permissions grant_row WHERE grant_row.authorization_id = a.id))))"""
    filters = f"""strpos(LOWER(COALESCE(a.display_name, '')), LOWER(%s)) > 0 AND (
        %s = 'all' OR (%s = 'none' AND NOT {backend_access}) OR (%s = 'any' AND {backend_access})
        OR (%s = 'owner' AND {owner_account}) OR (%s = ANY(%s::text[]) AND a.status = 'active' AND (
            {owner} OR (NOT {owner_account} AND EXISTS (SELECT 1 FROM admin_page_permissions grant_row
                              WHERE grant_row.authorization_id = a.id AND grant_row.permission = %s)))))"""
    filter_params = (query, *([permission_filter] * 5), sorted(PAGE_KEYS), permission_filter)
    with connection() as conn:
        counts = conn.execute(f"""
            SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE {backend_access}) AS backend,
                   COUNT(*) FILTER (WHERE {owner_account}) AS owners, COUNT(*) FILTER (WHERE {filters}) AS total_count
            FROM authorizations a LEFT JOIN admin_roles r ON r.authorization_id = a.id
        """, filter_params).fetchone()
        total_count = int(counts["total_count"] or 0)
        page, limit, offset = _page_window(page, total_count=total_count)
        rows = conn.execute(
            f"""
            WITH deleted_messages AS MATERIALIZED (
                SELECT chat_id,message_id FROM chat_messages WHERE is_deleted
            ), page_users AS MATERIALIZED (
                SELECT a.id FROM authorizations a LEFT JOIN admin_roles r ON r.authorization_id=a.id
                WHERE {filters} ORDER BY a.authorized_at DESC, a.id LIMIT %s OFFSET %s
            ), conversation_counts AS (
                {CONVERSATION_COUNTS_SQL}
            ), message_counts AS (
                SELECT
                    o.authorization_id,
                    COUNT(*) AS message_count,
                    COUNT(*) FILTER (WHERE o.sent_by_author) AS own_message_count
                FROM authorization_message_observations o
                JOIN page_users p ON p.id=o.authorization_id
                JOIN authorization_chats ac
                  ON ac.authorization_id = o.authorization_id AND ac.chat_id = o.chat_id
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE ac.is_visible AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND NOT EXISTS (SELECT 1 FROM deleted_messages m
                      WHERE m.chat_id=o.chat_id AND m.message_id=o.message_id)
                GROUP BY o.authorization_id
            ), latest_jobs AS (
                SELECT DISTINCT ON (authorization_id)
                    authorization_id, mode, status, scheduled_at, updated_at, pages_processed,
                    chats_discovered, discovery_status, last_progress_at, last_http_status, last_error_code
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
                a.scopes,
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
                j.pages_processed AS job_pages_processed,
                j.chats_discovered AS job_chats_discovered,
                j.discovery_status AS job_discovery_status,
                j.last_progress_at AS job_last_progress_at,
                j.last_http_status AS job_last_http_status,
                j.last_error_code AS job_last_error_code
            FROM authorizations a
            JOIN page_users p ON p.id=a.id
            LEFT JOIN admin_roles r ON r.authorization_id = a.id
            LEFT JOIN conversation_counts g ON g.authorization_id = a.id
            LEFT JOIN message_counts m ON m.authorization_id = a.id
            LEFT JOIN latest_jobs j ON j.authorization_id = a.id
            ORDER BY a.authorized_at DESC, a.id
            """,
            (*filter_params, limit, offset),
        ).fetchall()
    summary = {key: int(counts[key] or 0) for key in ("total", "backend", "owners")}
    summary["frontend"] = summary["total"] - summary["backend"]
    return {**_page_result([_decorate_user(row) for row in rows], page, total_count=total_count), "summary": summary}


def user_detail(authorization_id: UUID, conversation_page: int = 1, job_page: int = 1, *,
                include_conversations: bool = True, include_jobs: bool = True) -> dict[str, Any] | None:
    """Return one user plus their current conversation relationships and jobs."""

    with connection() as conn:
        user = conn.execute(
            f"""
            WITH deleted_messages AS MATERIALIZED (
                SELECT chat_id,message_id FROM chat_messages WHERE is_deleted
            ), conversation_counts AS (
                {CONVERSATION_COUNTS_SQL}
            ), message_counts AS (
                SELECT
                    o.authorization_id,
                    COUNT(*) AS message_count,
                    COUNT(*) FILTER (WHERE o.sent_by_author) AS own_message_count
                FROM authorization_message_observations o
                JOIN authorization_chats ac
                  ON ac.authorization_id = o.authorization_id AND ac.chat_id = o.chat_id
                JOIN chats c ON c.chat_id = ac.chat_id
                WHERE o.authorization_id = %s
                  AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND NOT EXISTS (SELECT 1 FROM deleted_messages m
                      WHERE m.chat_id=o.chat_id AND m.message_id=o.message_id)
                GROUP BY o.authorization_id
            ), latest_jobs AS (
                SELECT DISTINCT ON (authorization_id)
                    authorization_id, mode, status, scheduled_at, updated_at, pages_processed,
                    chats_discovered, discovery_status, last_progress_at, last_http_status, last_error_code
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
                a.scopes,
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
                j.pages_processed AS job_pages_processed,
                j.chats_discovered AS job_chats_discovered,
                j.discovery_status AS job_discovery_status,
                j.last_progress_at AS job_last_progress_at,
                j.last_http_status AS job_last_http_status,
                j.last_error_code AS job_last_error_code
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
        ) if include_conversations else 0
        job_total_count = int(
            conn.execute(
                "SELECT COUNT(*) AS total_count FROM sync_jobs WHERE authorization_id = %s",
                (authorization_id,),
            ).fetchone()["total_count"]
            or 0
        ) if include_jobs else 0
        conversation_page, conversation_limit, conversation_offset = _page_window(
            conversation_page, DETAIL_PAGE_SIZE, total_count=conversation_total_count
        )
        job_page, job_limit, job_offset = _page_window(job_page, DETAIL_PAGE_SIZE, total_count=job_total_count)

        conversations = conn.execute(
            """
            WITH page_chats AS MATERIALIZED (
                SELECT ac.* FROM authorization_chats ac
                LEFT JOIN chats c ON c.chat_id=ac.chat_id
                WHERE ac.authorization_id=%s AND ac.is_visible
                  AND COALESCE(c.chat_status, 'normal')='normal'
                ORDER BY LOWER(COALESCE(c.name, '')), ac.chat_id LIMIT %s OFFSET %s
            )
            SELECT
                ac.chat_id,
                c.name,
                c.external,
                c.chat_mode,
                ac.member_count,
                ac.member_count_status,
                ac.member_snapshot_status,
                ac.member_snapshot_checked_at,
                ac.member_snapshot_retry_at,
                (ac.member_snapshot_claim_id IS NOT NULL AND ac.member_snapshot_lease_until > NOW())
                    AS member_snapshot_in_flight,
                ac.sync_status,
                ac.backfill_complete,
                ac.backfill_started_at,
                ac.page_retry_at,
                (ac.backfill_page_token IS NOT NULL) AS backfill_has_next_page,
                (ac.incremental_page_token IS NOT NULL) AS incremental_has_next_page,
                ac.last_backfill_at,
                ac.last_incremental_at,
                COUNT(o.message_id) FILTER (WHERE m.message_id IS NULL) AS message_count
            FROM page_chats ac
            LEFT JOIN chats c ON c.chat_id = ac.chat_id
            LEFT JOIN authorization_message_observations o
              ON o.authorization_id = ac.authorization_id AND o.chat_id = ac.chat_id
            LEFT JOIN chat_messages m ON m.chat_id = o.chat_id AND m.message_id = o.message_id AND m.is_deleted
            GROUP BY
                ac.authorization_id, ac.chat_id, ac.member_count, ac.member_count_status,
                ac.member_snapshot_status, ac.member_snapshot_checked_at, ac.member_snapshot_retry_at,
                ac.member_snapshot_claim_id, ac.member_snapshot_lease_until,
                ac.sync_status, ac.backfill_complete, ac.backfill_started_at,
                ac.page_retry_at,
                ac.backfill_page_token, ac.incremental_page_token,
                ac.last_backfill_at, ac.last_incremental_at,
                c.name, c.external, c.chat_mode
            ORDER BY LOWER(COALESCE(c.name, '')), ac.chat_id
            """,
            (authorization_id, conversation_limit, conversation_offset),
        ).fetchall() if include_conversations else []
        jobs = conn.execute(
            """
            SELECT
                id, mode, status, pages_processed, chats_discovered, messages_indexed,
                own_messages_indexed, attempts, created_at, started_at, finished_at,
                scheduled_at, updated_at, discovery_status, last_progress_at, last_http_status, last_error_code
            FROM sync_jobs
            WHERE authorization_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (authorization_id, job_limit, job_offset),
        ).fetchall() if include_jobs else []

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
                classification.purpose AS group_purpose,
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
            LEFT JOIN chat_group_classifications classification ON classification.chat_id=c.chat_id
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
                j.discovery_status,
                j.last_progress_at,
                j.last_http_status,
                j.last_error_code,
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


CONTACT_DIRECTORY_SQL = f"""
    WITH directory AS (
        SELECT
            contact.id AS contact_id,
            contact.created_at, contact.updated_at,
            COALESCE(contact.display_name, primary_identity.display_name) AS display_name,
            primary_identity.open_id,
            CASE WHEN primary_identity.avatar_status='unavailable' THEN NULL
                 ELSE primary_identity.avatar_url END AS avatar_url,
            primary_identity.tenant_key,
            primary_identity.tenant_classification,
            identity_counts.lark_identity_count,
            identity_counts.last_seen_at,
            primary_role.role AS primary_role,
            primary_role.source AS classification_source,
            COALESCE(primary_role.role, 'unclassified') AS category,
            COALESCE(organization.status, 'unresolved') AS organization_status,
            organization.canonical_name AS organization_name,
            organization.organization_kind,
            CASE WHEN primary_role.role IS NULL THEN ARRAY[]::text[] ELSE ARRAY[primary_role.role] END
                AS confirmed_roles
        FROM contacts contact
        LEFT JOIN LATERAL (
            SELECT identity.open_id, identity.display_name, identity.avatar_url, identity.avatar_status,
                identity.tenant_key, identity.tenant_classification
            FROM lark_identities identity
            WHERE identity.contact_id = contact.id AND identity.identity_type = 'user'
            ORDER BY identity.last_seen_at DESC, identity.open_id
            LIMIT 1
        ) AS primary_identity ON TRUE
        LEFT JOIN LATERAL (
            SELECT job.status, job.result, job.finished_at
            FROM contact_assessments job
            WHERE job.contact_id=contact.id AND job.policy_version='{CONTACT_IDENTITY_POLICY}'
            ORDER BY job.created_at DESC,job.id DESC LIMIT 1
        ) AS assessment ON TRUE
        LEFT JOIN LATERAL (
            SELECT role.role,role.source
            FROM contact_roles role
            WHERE role.contact_id=contact.id AND role.status='confirmed'
              AND (role.source NOT IN ('ai_auto_confirmed','lark_internal_tenant')
                OR role.source='lark_internal_tenant' AND role.role='internal_colleague' AND EXISTS (
                    SELECT 1 FROM lark_identities trusted WHERE trusted.contact_id=contact.id
                      AND trusted.identity_type='user' AND trusted.tenant_classification='internal')
                OR role.source='ai_auto_confirmed' AND role.is_primary AND assessment.status='succeeded'
                  AND assessment.result->>'reason_code'='classified'
                  AND assessment.result->'roles'->0->>'role'=role.role)
            ORDER BY CASE WHEN role.source NOT IN ('ai_auto_confirmed','lark_internal_tenant') THEN 0
                          WHEN role.source='lark_internal_tenant' THEN 1 ELSE 2 END,
                role.is_primary DESC,role.reviewed_at DESC NULLS LAST,role.updated_at DESC,role.role
            LIMIT 1
        ) AS primary_role ON TRUE
        LEFT JOIN LATERAL (
            SELECT link.status, organization.canonical_name, organization.organization_kind
            FROM contact_organization_links link
            JOIN organizations organization ON organization.id = link.organization_id
            WHERE link.contact_id = contact.id AND link.status <> 'rejected'
            ORDER BY CASE link.status WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                link.last_seen_at DESC, organization.canonical_name
            LIMIT 1
        ) AS organization ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS lark_identity_count, MAX(identity.last_seen_at) AS last_seen_at
            FROM lark_identities identity
            WHERE identity.contact_id = contact.id AND identity.identity_type = 'user'
        ) AS identity_counts ON TRUE
    )
"""


def contacts(
    page: int = 1,
    category: str = "all",
    organization_status: str = "all",
    organization_query: str = "",
    query: str = "",
) -> dict[str, Any]:
    """Return people filtered by category, organization state, and organization name."""

    category = normalize_contact_category(category)
    organization_status = normalize_organization_status(organization_status)
    organization_query = normalize_organization_query(organization_query)
    query = normalize_organization_query(query)
    filters = (category, category, organization_status, organization_status, organization_query, organization_query,
               query, query, query)
    with connection() as conn:
        # Materialize once: global category totals and the filtered page share
        # the same identity/classification snapshot without a cross-request cache.
        page, _, _ = _page_window(page)
        rows = conn.execute(
            f"""
            {CONTACT_DIRECTORY_SQL}
            , counts AS (
                SELECT category,COUNT(*) AS count
                FROM directory GROUP BY category
            ), filtered AS MATERIALIZED (
            SELECT * FROM directory
            WHERE (%s = 'all' OR category = %s)
              AND (%s = 'all' OR organization_status = %s)
              AND (
                  %s = '' OR EXISTS (
                      SELECT 1
                      FROM contact_organization_links organization_link
                      JOIN organizations organization_filter
                        ON organization_filter.id = organization_link.organization_id
                      WHERE organization_link.contact_id = directory.contact_id
                        AND organization_link.status <> 'rejected'
                        AND POSITION(LOWER(%s) IN LOWER(organization_filter.canonical_name)) > 0
                  )
              )
              AND (%s='' OR POSITION(LOWER(%s) IN LOWER(COALESCE(display_name,'')))>0
                OR EXISTS(SELECT 1 FROM contact_social_accounts l JOIN social_accounts s ON s.id=l.social_account_id
                  WHERE l.contact_id=directory.contact_id AND l.status='confirmed' AND l.relationship='owner'
                  AND s.platform='tiktok' AND POSITION(LOWER(%s) IN LOWER(s.normalized_handle))>0))
            ), totals AS (
                SELECT COUNT(*) AS total_count FROM filtered
            ), page_rows AS (
                SELECT * FROM filtered
                ORDER BY last_seen_at DESC NULLS LAST, LOWER(display_name), contact_id
                LIMIT %s OFFSET (LEAST(%s, GREATEST(1,
                    (SELECT CEIL(total_count::numeric / %s) FROM totals))) - 1)::int * %s
            )
            SELECT page_rows.*, totals.total_count,
                (SELECT COALESCE(jsonb_agg(to_jsonb(counts)), '[]'::jsonb) FROM counts) AS category_counts
            FROM totals LEFT JOIN page_rows ON TRUE
            ORDER BY last_seen_at DESC NULLS LAST, LOWER(display_name), contact_id
            """,
            (*filters, LIST_PAGE_SIZE, page, LIST_PAGE_SIZE, LIST_PAGE_SIZE),
        ).fetchall()
        total_count = int(rows[0]["total_count"])
        counts = rows[0]["category_counts"]
        page, _, _ = _page_window(page, total_count=total_count)
        rows = [row for row in rows if row["contact_id"] is not None]
        # Conversation/message counts are metadata, not model input. Count only this page's people.
        ids = [row["contact_id"] for row in rows]
        handles = contact_handles.for_contacts(conn, ids)
        activity = conn.execute("""
            WITH identities AS (SELECT contact_id,open_id FROM lark_identities WHERE contact_id=ANY(%s::uuid[])),
            messages AS MATERIALIZED (
                SELECT identity.contact_id,message.chat_id,message.is_deleted
                FROM identities identity JOIN chat_messages message ON message.sender_open_id=identity.open_id
            ),
            conversations AS (
                SELECT identity.contact_id,member.chat_id FROM chat_members member
                JOIN identities identity USING(open_id)
                UNION SELECT contact_id,chat_id FROM messages
                UNION SELECT identity.contact_id,association.chat_id FROM authorization_chats association
                JOIN identities identity ON identity.open_id=association.p2p_counterpart_open_id
                WHERE association.is_visible
            )
            SELECT person.contact_id,
                (SELECT COUNT(*) FROM conversations c WHERE c.contact_id=person.contact_id) AS conversation_count,
                (SELECT COUNT(*) FROM messages m WHERE m.contact_id=person.contact_id AND NOT m.is_deleted)
                    AS message_count
            FROM (SELECT DISTINCT contact_id FROM identities) person
            """, (ids,)).fetchall() if ids else []
    activity_by_id = {row["contact_id"]: row for row in activity}
    result = _page_result([_decorate_contact({**row, **activity_by_id.get(row["contact_id"], {}),
                                             'handles': handles.get(row['contact_id'], [])})
                           for row in rows], page, total_count=total_count)
    result["identity_summary"] = _identity_summary(counts)
    return result


def _identity_summary(rows: list[dict]) -> dict[str, Any]:
    categories = dict.fromkeys(("unclassified", *CONTACT_ROLE_CODES), 0)
    for row in rows:
        categories[row["category"]] += int(row["count"])
    return {"total": sum(categories.values()), "categories": categories}


def organization_sync_status() -> dict[str, Any]:
    """Return a safe, body-free status for the people-directory header."""

    with connection() as conn:
        row = conn.execute(
            "SELECT status, checked_at, succeeded_at, detail FROM organization_sync_state WHERE singleton"
        ).fetchone()
    item = dict(row or {"status": "pending", "detail": "尚未检查关联组织权限。"})
    labels = {
        "pending": ("等待关联组织同步", "secondary"),
        "succeeded": ("关联组织已同步", "success"),
        "unavailable": ("关联组织不可用", "warning"),
        "failed": ("关联组织同步待重试", "warning"),
    }
    item["status_label"], item["status_badge"] = labels.get(item.get("status"), ("状态未知", "secondary"))
    item["checked_at_label"] = _format_time(item.get("checked_at"))
    item["detail"] = item.get("detail") or "尚未检查关联组织权限。"
    return item


def contact_detail(open_id: str, conversation_page: int = 1, *,
                   include_conversations: bool = True) -> dict[str, Any] | None:
    """Return one stable Lark contact with role, organization, and conversation facts."""

    with connection() as conn:
        contact = conn.execute(
            f"""{CONTACT_DIRECTORY_SQL}
            SELECT * FROM directory WHERE contact_id=(
                SELECT contact_id FROM lark_identities WHERE open_id=%s AND identity_type='user')""",
            (open_id,),
        ).fetchone()
        if not contact:
            return None

        contact_id = contact["contact_id"]
        tenant_population = 0
        if contact["tenant_key"]:
            tenant_population = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT contact_id) AS total_count
                    FROM lark_identities
                    WHERE tenant_key = %s AND identity_type = 'user' AND contact_id IS NOT NULL
                    """,
                    (contact["tenant_key"],),
                ).fetchone()["total_count"]
                or 0
            )
        identities = conn.execute(
            """
            SELECT open_id, display_name, tenant_key, tenant_classification, first_seen_at, last_seen_at
            FROM lark_identities
            WHERE contact_id = %s AND identity_type = 'user'
            ORDER BY last_seen_at DESC, open_id
            """,
            (contact_id,),
        ).fetchall()
        organizations = conn.execute(
            """
            SELECT
                organization.canonical_name, organization.organization_kind, organization.verification_status,
                link.relationship, link.status, link.source, link.confidence, link.last_seen_at
            FROM contact_organization_links link
            JOIN organizations organization ON organization.id = link.organization_id
            WHERE link.contact_id = %s AND link.status <> 'rejected'
            ORDER BY CASE link.status WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                link.last_seen_at DESC, organization.canonical_name
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
        ) if include_conversations else 0
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
                classification.purpose AS group_purpose,
                member_counts.member_count,
                member_counts.member_count_status,
                message_counts.message_count,
                message_counts.contact_message_count,
                message_counts.last_message_at
            FROM conversation_ids ids
            JOIN chats chat ON chat.chat_id = ids.chat_id
            LEFT JOIN chat_group_classifications classification ON classification.chat_id=chat.chat_id
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
        ).fetchall() if include_conversations else []

    return {
        "contact": _decorate_contact(contact),
        "tenant_population": tenant_population,
        "identities": [_decorate_lark_identity(row) for row in identities],
        "organizations": [_decorate_contact_organization(row) for row in organizations],
        "conversations": _page_result(
            [_decorate_contact_conversation(row) for row in conversations],
            conversation_page,
            DETAIL_PAGE_SIZE,
            total_count=conversation_total_count,
        ),
    }


def normalize_message_conversation_query(value: str | None) -> str:
    """Keep the admin conversation search bounded and predictable."""

    return (value or "").strip()[:MAX_MESSAGE_CONVERSATION_QUERY_LENGTH]


def normalize_message_conversation_type(value: str | None) -> str:
    """Return a supported message-directory type filter."""

    return value if value in MESSAGE_CONVERSATION_TYPES else "all"


def normalize_contact_category(value: str | None) -> str:
    """Return one supported people-directory main-category filter."""

    return value if value in CONTACT_CATEGORIES else "all"


def normalize_organization_status(value: str | None) -> str:
    """Return one supported organization-verification filter."""

    return value if value in ORGANIZATION_STATUSES else "all"


def normalize_organization_query(value: str | None) -> str:
    """Keep the organization-name filter bounded without inventing a search index."""

    return " ".join((value or "").split())[:MAX_ORGANIZATION_QUERY_LENGTH]


def message_conversations(
    authorization_id: UUID,
    page: int = 1,
    query: str = "",
    chat_type: str = "all",
) -> dict[str, Any]:
    """Return one filtered, twenty-conversation page for the authorized content view."""

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
            f"""
            WITH visible_messages AS (
                SELECT m.*, o.sent_by_author, c.name, COALESCE(c.chat_mode, 'group') AS chat_mode
                FROM authorizations a
                JOIN authorization_chats ac ON ac.authorization_id = a.id
                JOIN chats c ON c.chat_id = ac.chat_id
                JOIN authorization_message_observations o
                  ON o.authorization_id = ac.authorization_id AND o.chat_id = ac.chat_id
                JOIN chat_messages m ON m.chat_id = o.chat_id AND m.message_id = o.message_id
                WHERE a.id = %s
                  AND a.status = 'active'
                  AND {BODY_READ_SCOPE_SQL}
                  AND a.body_consent_at IS NOT NULL
                  AND o.observed_at >= a.body_consent_at
                  AND ac.is_visible AND ac.sync_enabled
                  AND ac.sync_status IS DISTINCT FROM 'not_readable'
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
            f"""
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
                  AND a.status = 'active'
                  AND {BODY_READ_SCOPE_SQL}
                  AND a.body_consent_at IS NOT NULL
                  AND o.observed_at >= a.body_consent_at
                  AND ac.is_visible AND ac.sync_enabled
                  AND ac.sync_status IS DISTINCT FROM 'not_readable'
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

        names = mention_names(conn, rows[:MESSAGE_PAGE_SIZE], lock=False)
    cipher = message_body_cipher(get_settings().token_encryption_key)
    items = [_decorate_message(row, cipher, names) for row in reversed(rows[:MESSAGE_PAGE_SIZE])]
    return {
        "metrics": {key: int(metrics[key] or 0) for key in metrics},
        **_page_result(items, page, MESSAGE_PAGE_SIZE, total_count=total_count),
    }


def record_message_view(viewer_id: UUID, target_authorization_id: UUID, chat_id: str | None) -> None:
    """Audit an authorized content-page view without ever placing message text in the log."""

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
    for field in (
        "authorized_at",
        "updated_at",
        "last_synced_at",
        "job_scheduled_at",
        "job_updated_at",
        "job_last_progress_at",
    ):
        item[f"{field}_label"] = _format_time(item.get(field))
    item["authorization_status_label"], item["authorization_status_badge"] = _authorization_status(
        item.get("authorization_status")
    )
    item["job_status_label"], item["job_status_badge"] = _job_status(
        item.get("job_status"),
        item.get("job_mode"),
        item.get("job_scheduled_at"),
        item.get("job_last_http_status"),
        item.get("job_last_error_code"),
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
    item["message_page_label"] = _message_page_activity(item)
    item["member_snapshot_label"] = _member_snapshot_activity(item)
    return item


def _decorate_chat(row: dict[str, Any]) -> dict[str, Any]:
    from .creator_group_evidence import PURPOSES as GROUP_PURPOSES

    item = dict(row)
    item["group_purpose_label"] = GROUP_PURPOSES.get(item.get("group_purpose"), "未确认")
    item["group_purpose_badge"] = GROUP_PURPOSE_BADGES.get(item.get("group_purpose"), "secondary")
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


def _decorate_message(row: dict[str, Any], cipher: TokenCipher, names=None) -> dict[str, Any]:
    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_mode_label"] = _chat_mode_label(item["chat_mode"])
    item["chat_name"] = _conversation_name(item.get("name"), item["chat_mode"], item.get("chat_id"))
    item["sender_label"] = (
        "本人" if item.get("sent_by_author") else item.get("sender_display_name") or "会话对方 / 群成员"
    )
    item["created_at_label"] = _message_time(item.get("message_create_time"))
    created_at = item.get("message_create_time")
    item["created_date_label"] = (
        created_at.astimezone(LONDON_TIME_ZONE).strftime("%Y-%m-%d") if created_at else "日期未知"
    )
    item["message_type_label"] = _message_type_label(item.get("message_type"))
    try:
        body = cipher.decrypt(item["body_ciphertext"])
    except (InvalidTag, UnicodeDecodeError, ValueError):
        item["content"] = "（正文无法读取）"
    else:
        item["content"] = _readable_body(body, item.get("message_type"), item.get("mentions_json"), names)
    return item


def _message_type_label(message_type: str | None) -> str:
    return {"text": "文字", "post": "富文本", "interactive": "卡片", "image": "图片",
            "file": "文件", "audio": "语音", "media": "视频", "sticker": "表情",
            "merge_forward": "合并转发", "share_chat": "分享群聊", "share_user": "分享联系人"
            }.get(message_type, "其他消息")


def _conversation_name(name: Any, chat_mode: str, chat_id: Any) -> str:
    if isinstance(name, str) and name:
        return name
    if chat_mode == "p2p":
        return f"未命名私聊 · {str(chat_id)[-8:]}"
    return "未命名群聊"


def _decorate_job(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "未命名用户"
    for field in ("created_at", "started_at", "finished_at", "scheduled_at", "updated_at", "last_progress_at"):
        item[f"{field}_label"] = _format_time(item.get(field))
    item["mode_label"] = "首次回填" if item.get("mode") == "backfill" else "定时更新"
    item["status_label"], item["status_badge"] = _job_status(
        item.get("status"),
        item.get("mode"),
        item.get("scheduled_at"),
        item.get("last_http_status"),
        item.get("last_error_code"),
    )
    item["discovery_label"] = {
        "pending": "等待枚举会话",
        "running": "正在枚举会话",
        "succeeded": "会话目录已就绪",
    }.get(item.get("discovery_status"), "目录状态未知")
    return item


def _decorate_contact(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["display_name"] = item.get("display_name") or "Lark 参与者（姓名未返回）"
    item["avatar_url"] = safe_avatar_url(item.get("avatar_url"))
    roles = item.get("confirmed_roles")
    roles = roles if isinstance(roles, list | tuple) else ()
    item["confirmed_role_labels"] = [ROLE_LABELS[role] for role in roles if role in ROLE_LABELS]
    category = item.get("category") or item.get("primary_role") or "unclassified"
    item["category"] = category
    item["private_chat_url"] = private_chat_url(item.get("open_id")) if category == "creator" else None
    item["category_label"] = role_label(None if category == "unclassified" else category)
    item["category_badge"] = {
        "creator": "success",
        "partner_contact": "azure",
        "internal_colleague": "primary",
        "merchant_contact": "warning",
        "platform_contact": "purple",
        "other_external_contact": "secondary",
        "unclassified": "secondary",
    }.get(category, "secondary")
    item["is_confirmed"] = category != "unclassified"
    source = item.get("classification_source")
    item["classification_source_label"] = (
        "已核实同事" if source == "lark_internal_tenant" else
        "AI" if source == "ai_auto_confirmed" else "人工 / 已确认资料" if source else "—"
    )
    item["organization_name"] = item.get("organization_name") or "未解析"
    item["organization_status"] = item.get("organization_status") or "unresolved"
    item["organization_status_label"], item["organization_status_badge"] = _organization_status(
        item["organization_status"]
    )
    item["tenant_classification_label"], item["tenant_classification_badge"] = _tenant_classification(
        item.get("tenant_classification")
    )
    for field in ("lark_identity_count", "conversation_count", "message_count"):
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


def _decorate_contact_organization(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["canonical_name"] = item.get("canonical_name") or "未命名组织"
    item["status_label"], item["status_badge"] = _organization_status(item.get("status"))
    item["organization_kind_label"] = ORGANIZATION_KIND_LABELS.get(item.get("organization_kind"), "类型未标注")
    relationships = {
        "tenant_affiliation": "Lark 租户关联",
        "employee": "成员关系",
        "representative": "机构代表",
        "agent": "代理关系",
        "owner": "所有者",
        "unknown": "关系未确认",
    }
    item["relationship_label"] = relationships.get(item.get("relationship"), "关系未确认")
    item["source_label"] = {
        "lark_trust_party": "Lark 关联组织",
        "crm": "CRM",
        "manual": "人工",
        "ai_candidate": "AI 候选",
    }.get(item.get("source"), "来源未标注")
    item["last_seen_at_label"] = _format_time(item.get("last_seen_at"))
    return item


def _decorate_contact_conversation(row: dict[str, Any]) -> dict[str, Any]:
    from .creator_group_evidence import PURPOSES as GROUP_PURPOSES

    item = dict(row)
    item["chat_mode"] = item.get("chat_mode") or "group"
    item["chat_mode_label"] = _chat_mode_label(item["chat_mode"])
    item["group_purpose_label"] = (
        "—" if item["chat_mode"] == "p2p" else GROUP_PURPOSES.get(item.get("group_purpose"), "未确认")
    )
    item["group_purpose_badge"] = (
        None if item["chat_mode"] == "p2p" else GROUP_PURPOSE_BADGES.get(item.get("group_purpose"), "secondary")
    )
    item["name"] = _conversation_name(item.get("name"), item["chat_mode"], item.get("chat_id"))
    item["member_count_label"] = (
        "—" if item["chat_mode"] == "p2p" else _member_count(item.get("member_count"), item.get("member_count_status"))
    )
    item["message_count"] = int(item.get("message_count") or 0)
    item["contact_message_count"] = int(item.get("contact_message_count") or 0)
    item["last_message_at_label"] = _format_time(item.get("last_message_at"))
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

    if job_status in {"queued", "running"} and _is_lark_rate_limited(
        facts.get("job_last_http_status"), facts.get("job_last_error_code")
    ):
        return "Lark 限流暂停", "warning", "Lark 暂时要求降低请求速率；恢复后会自动继续。"

    if job_status == "running" and facts.get("job_discovery_status") in {"pending", "running"}:
        return "正在发现会话", "primary", "正在建立当前可见会话目录；消息回填会在目录完成后并发开始。"

    if total == 0:
        if job_status in {"queued", "running"}:
            return "正在发现会话", "primary", "正在从 Lark 建立可同步会话目录。"
        return "暂无可同步会话", "secondary", "Lark 尚未返回可同步的群聊或私聊。"

    if completed < total:
        delayed = job_status == "queued" and isinstance(scheduled_at, datetime) and scheduled_at > current
        label = "首次同步中" if job_status == "running" else "等待会话重试" if delayed else "等待首次同步"
        parts = [f"已完成 {completed}/{total} 个可同步会话"]
        if syncing:
            parts.append(f"{syncing} 个正在读取")
        if waiting:
            parts.append(f"{waiting} 个等待处理")
        if unreadable:
            parts.append(f"{unreadable} 个 Lark 未允许读取")
        if archived:
            parts.append(f"另有 {archived} 个 Lark 已归档/解散会话不计划回填")
        if delayed:
            parts.append(f"下一次会话重试于 {_format_time(scheduled_at)}")
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
    last_http_status: int | None = None,
    last_error_code: int | None = None,
) -> tuple[str, str]:
    if value in {"queued", "running"} and _is_lark_rate_limited(last_http_status, last_error_code):
        return "Lark 限流暂停", "warning"
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


def _is_lark_rate_limited(http_status: Any, error_code: Any) -> bool:
    return http_status == 429 or error_code == 99991400


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


def _organization_status(value: str | None) -> tuple[str, str]:
    mapping = {
        "verified": ("已验证", "success"),
        "candidate": ("待核验", "warning"),
        "unresolved": ("未解析", "secondary"),
    }
    return mapping.get(value or "", ("组织状态未知", "secondary"))


def _member_count(count: int | None, status: str | None) -> str:
    if count is not None:
        return str(count)
    if status == "pending":
        return "正在获取"
    return "Lark 未返回"


def _message_page_activity(item: dict[str, Any]) -> str:
    if item.get("sync_status") == "failed_retryable" and item.get("page_retry_at"):
        return f"等待重试于 {_format_time(item.get('page_retry_at'))}"
    if not item.get("backfill_complete"):
        if item.get("sync_status") == "syncing":
            return "正在读取当前消息页"
        if item.get("backfill_has_next_page"):
            return "等待续读下一页"
        return "回填排队"
    if item.get("incremental_has_next_page"):
        return "正在读取增量页" if item.get("sync_status") == "syncing" else "等待增量续读"
    return "本轮消息已完成"


def _member_snapshot_activity(item: dict[str, Any]) -> str:
    if item.get("chat_mode") == "p2p":
        return "私聊不读取群成员目录"
    if item.get("member_snapshot_in_flight"):
        return "正在低优先刷新"
    status = item.get("member_snapshot_status")
    if status == "pending" and item.get("member_snapshot_retry_at"):
        return f"等待重试于 {_format_time(item.get('member_snapshot_retry_at'))}"
    if status == "succeeded":
        return f"已刷新于 {_format_time(item.get('member_snapshot_checked_at'))}"
    if status == "partial":
        return "Lark 仅返回部分成员资料"
    if status == "unavailable":
        return "Lark 未返回成员目录"
    return "等待消息回填空闲后刷新"


def _decorate_lark_admission(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    paused_until = item.get("paused_until")
    in_flight = int(item.get("in_flight") or 0)
    if isinstance(paused_until, datetime) and paused_until > datetime.now(UTC):
        return {
            "label": "Lark 限流暂停中",
            "badge": "warning",
            "detail": f"全局请求将在 {_format_time(paused_until)} 后自动恢复；当前在途请求 {in_flight} 个。",
        }
    return {
        "label": "Lark 请求调度正常",
        "badge": "success",
        "detail": f"当前在途请求 {in_flight} 个；累计限流恢复 {int(item.get('rate_limited_count') or 0)} 次。",
    }


def _message_count_label(count: int | None) -> str:
    return f"{int(count or 0)} 条消息" if count else "尚无消息"

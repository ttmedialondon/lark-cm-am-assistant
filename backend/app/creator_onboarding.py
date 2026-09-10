"""New OAuth registrations wait for local history; they never send it to AI."""

from __future__ import annotations

from uuid import UUID

from . import creator_workflow_ai as ai
from .db import connection


def register_initial_scan(conn, authorization_id):
    """OAuth caller owns the transaction; re-login must not restart a scan."""
    conn.execute(
        """INSERT INTO creator_workflow_onboarding(authorization_id,next_check_at)
           VALUES(%s,NOW()) ON CONFLICT(authorization_id) DO NOTHING""",
        (authorization_id,),
    )


def get_status(conn, authorization_id):
    row = conn.execute(
        """SELECT status,total_chats,checked_chats,queued_chats,truncated_chats,
                  registered_at,started_at,finished_at,window_start,window_end,error_message
           FROM creator_workflow_onboarding WHERE authorization_id=%s""",
        (authorization_id,),
    ).fetchone()
    return dict(row) if row else None


def advance_initial_scans():
    """Metadata-only readiness check. Explicit preflight/start owns all paid work."""
    with connection() as conn, conn.transaction():
        rows = conn.execute(
            """SELECT * FROM creator_workflow_onboarding
               WHERE status='waiting_sync' AND next_check_at<=NOW()
               ORDER BY next_check_at,authorization_id LIMIT 5 FOR UPDATE SKIP LOCKED"""
        ).fetchall()
        for row in rows:
            auth = conn.execute(
                "SELECT status,assignment_eligible,body_consent_at FROM authorizations WHERE id=%s",
                (row["authorization_id"],),
            ).fetchone()
            status, message = "waiting_sync", "等待首次历史消息同步完成；尚未调用 AI。"
            if not auth or auth["status"] != "active":
                status, message = "cancelled", "Lark 授权已失效；未启动 AI 核对。"
            elif not auth["assignment_eligible"] or not auth["body_consent_at"]:
                message = "等待 CM 资格和消息正文授权；尚未调用 AI。"
            elif conn.execute(
                """SELECT 1 FROM sync_jobs WHERE authorization_id=%s AND mode='backfill'
                   AND status='succeeded' AND finished_at>=%s AND finished_at>=%s LIMIT 1""",
                (row["authorization_id"], row["registered_at"], auth["body_consent_at"]),
            ).fetchone():
                status, message = "ready", "历史同步已就绪；请先准备核对，再明确启动 AI。"
            conn.execute(
                """UPDATE creator_workflow_onboarding SET status=%s,error_message=%s,
                   next_check_at=CASE WHEN %s='waiting_sync' THEN NOW()+INTERVAL '30 seconds' ELSE NULL END,
                   finished_at=CASE WHEN %s='waiting_sync' THEN NULL ELSE NOW() END
                   WHERE authorization_id=%s""",
                (status, message, status, status, row["authorization_id"]),
            )


def retry_initial_scan(actor_id, responsible_id):
    """Compatibility fence: old links must never resurrect whole-chat jobs."""
    with connection() as conn:
        ai._authorize(conn, UUID(str(actor_id)), UUID(str(responsible_id)))
    raise ai.ScanInputError("旧的全量聊天扫描已停用，请重新准备按用户核对。")

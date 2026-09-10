"""One durable 90-day candidate pass, registered only by a new OAuth insert."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from . import creator_workflow_ai as ai
from .db import connection


def register_initial_scan(conn, authorization_id):
    """OAuth caller owns the transaction and must call ONLY for its new insert."""
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
    if not row:
        return None
    return {
        **dict(row),
        "message_limit": ai.MAX_MESSAGES,
        "message_char_limit": ai.MAX_BODY_CHARS,
        "history_days": 90,
    }


def _stop(conn, row, status, message):
    conn.execute(
        """UPDATE creator_workflow_ai_tasks t SET status='cancelled',lease_until=NULL,
                  error_message=%s,finished_at=NOW()
           FROM creator_workflow_ai_runs r WHERE r.id=t.run_id AND r.onboarding_authorization_id=%s
             AND t.status IN ('queued','running')""",
        (message, row["authorization_id"]),
    )
    if row["run_id"]:
        ai._refresh_run(conn, row["run_id"])
    conn.execute(
        """UPDATE creator_workflow_onboarding SET status=%s,error_message=%s,
           next_check_at=NULL,finished_at=NOW() WHERE authorization_id=%s""",
        (status, message, row["authorization_id"]),
    )


def advance_initial_scans():
    """Worker metadata tick; no model calls and no automatic retries of failures."""
    with connection() as conn, conn.transaction():
        rows = conn.execute(
            """SELECT * FROM creator_workflow_onboarding
               WHERE status IN ('waiting_sync','scanning') AND next_check_at<=NOW()
               ORDER BY next_check_at,authorization_id LIMIT 5 FOR UPDATE SKIP LOCKED"""
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE creator_workflow_onboarding SET next_check_at=NOW()+INTERVAL '15 seconds'
                   WHERE authorization_id=%s""",
                (row["authorization_id"],),
            )
            auth = conn.execute(
                "SELECT status,assignment_eligible,body_consent_at FROM authorizations WHERE id=%s",
                (row["authorization_id"],),
            ).fetchone()
            if not auth or auth["status"] != "active":
                _stop(conn, row, "cancelled", "Lark 授权已失效；首次筛选已停止，恢复后请手动重试。")
                continue
            if not auth["assignment_eligible"] or not auth["body_consent_at"]:
                if row["started_at"]:
                    _stop(conn, row, "cancelled", "CM 资格或正文授权已变化；首次筛选已停止。")
                else:
                    conn.execute(
                        "UPDATE creator_workflow_onboarding SET error_message=%s WHERE authorization_id=%s",
                        ("等待 CM 资格和消息正文授权。", row["authorization_id"]),
                    )
                continue
            if row["started_at"] and row["consent_at"] != auth["body_consent_at"]:
                _stop(conn, row, "cancelled", "正文授权已更新；原批次已停止，请手动重新核对。")
                continue
            if not ai._enabled(conn):
                conn.execute(
                    "UPDATE creator_workflow_onboarding SET error_message=%s WHERE authorization_id=%s",
                    ("等待主人配置并开启达人候选 AI；首次请求仍保留。", row["authorization_id"]),
                )
                continue
            if not row["started_at"]:
                sync = conn.execute(
                    """SELECT 1 FROM sync_jobs WHERE authorization_id=%s AND mode='backfill'
                       AND status='succeeded' AND finished_at>=%s AND finished_at>=%s LIMIT 1""",
                    (row["authorization_id"], row["registered_at"], auth["body_consent_at"]),
                ).fetchone()
                if not sync:
                    conn.execute(
                        "UPDATE creator_workflow_onboarding SET error_message=%s WHERE authorization_id=%s",
                        ("等待首次会话发现与历史消息同步完成；尚未调用 AI。", row["authorization_id"]),
                    )
                    continue
                end = datetime.now(UTC)
                total = conn.execute(
                    """SELECT COUNT(*) AS n FROM authorization_chats ac JOIN chats c USING(chat_id)
                       WHERE ac.authorization_id=%s AND ac.is_visible AND ac.sync_enabled
                         AND ac.discovered_at<=%s AND COALESCE(c.chat_status,'normal')='normal'""",
                    (row["authorization_id"], end),
                ).fetchone()["n"]
                conn.execute(
                    """UPDATE creator_workflow_onboarding SET status='scanning',started_at=%s,
                       window_start=%s,window_end=%s,consent_at=%s,total_chats=%s,error_message=NULL
                       WHERE authorization_id=%s""",
                    (end, end - timedelta(days=90), end, auth["body_consent_at"], total, row["authorization_id"]),
                )
                row = {
                    **row,
                    "started_at": end,
                    "window_start": end - timedelta(days=90),
                    "window_end": end,
                    "consent_at": auth["body_consent_at"],
                }
            if row["run_id"]:
                run = conn.execute(
                    "SELECT status FROM creator_workflow_ai_runs WHERE id=%s", (row["run_id"],)
                ).fetchone()
                if run and run["status"] in {"queued", "running"}:
                    continue
                if not run or run["status"] != "succeeded":
                    _stop(conn, row, "failed", "本批有失败或资料变化，首次筛选未完成；请手动重试，不会无限调用。")
                    continue
            batch = ai.queue_initial_batch(conn, row)
            if batch is None:  # A separately requested scan currently owns this CM.
                continue
            if batch["checked"] == 0:
                conn.execute(
                    """UPDATE creator_workflow_onboarding SET status='succeeded',finished_at=NOW(),
                       next_check_at=NULL,error_message=NULL WHERE authorization_id=%s""",
                    (row["authorization_id"],),
                )
                continue
            conn.execute(
                """UPDATE creator_workflow_onboarding SET run_id=%s,last_chat_id=%s,
                   checked_chats=checked_chats+%s,queued_chats=queued_chats+%s,
                   truncated_chats=truncated_chats+%s,error_message=NULL WHERE authorization_id=%s""",
                (
                    batch["run_id"],
                    batch["last_chat_id"],
                    batch["checked"],
                    batch["queued"],
                    batch["truncated"],
                    row["authorization_id"],
                ),
            )


def retry_initial_scan(actor_id, responsible_id):
    """An explicit CM/owner retry only; successful pages are never recharged."""
    actor_id, responsible_id = UUID(str(actor_id)), UUID(str(responsible_id))
    with connection() as conn, conn.transaction():
        ai._authorize(conn, actor_id, responsible_id)
        row = conn.execute(
            "SELECT * FROM creator_workflow_onboarding WHERE authorization_id=%s FOR UPDATE",
            (responsible_id,),
        ).fetchone()
        if not row or row["status"] not in {"failed", "cancelled"}:
            return get_status(conn, responsible_id)
        consent = conn.execute("SELECT body_consent_at FROM authorizations WHERE id=%s", (responsible_id,)).fetchone()
        if row["consent_at"] and row["consent_at"] != consent["body_consent_at"]:
            raise ai.ScanInputError("正文授权已变化，旧首次筛选不可重放；请使用当前范围的手动扫描。")
        if row["run_id"]:
            conn.execute(
                """UPDATE creator_workflow_ai_tasks SET status='queued',scheduled_at=NOW(),error_message=NULL
                   WHERE run_id=%s AND status IN ('failed','invalid','cancelled')""",
                (row["run_id"],),
            )
            ai._refresh_run(conn, row["run_id"])
        conn.execute(
            """UPDATE creator_workflow_onboarding SET status=%s,next_check_at=NOW(),finished_at=NULL,
               error_message=NULL WHERE authorization_id=%s""",
            ("scanning" if row["started_at"] else "waiting_sync", responsible_id),
        )
        return get_status(conn, responsible_id)

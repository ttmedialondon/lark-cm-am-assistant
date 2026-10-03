"""Shared OpenAI admission state for the durable AI queues.

The worker stays single-process for Lark sync.  This module only coordinates
the small number of paid OpenAI requests it may hand to worker threads.
"""

from __future__ import annotations

import math
import re
from typing import Any

import psycopg

from .db import connection

MAX_CONCURRENT_REQUESTS = 3
_ADMISSION_LOCK = 541_195_270
_TEMPORARY_RATE_LIMIT_MESSAGE = "OpenAI 请求暂时限速；系统将按服务端给出的时间自动重试。"
_QUOTA_PAUSE_MESSAGE = "OpenAI 项目因配额或账单问题已暂停；请处理后由所有者恢复队列。"


def can_claim(conn: psycopg.Connection) -> bool:
    """Atomically decide whether any AI queue may claim one more task."""

    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_LOCK,))
    state = conn.execute(
        """
        SELECT paused_at, not_before > NOW() AS throttled
        FROM ai_admission_state
        WHERE id = TRUE
        FOR UPDATE
        """
    ).fetchone()
    if not state or state["paused_at"] is not None or state["throttled"]:
        return False
    active = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM (
            SELECT id FROM summary_reply_tasks
            WHERE status = 'running' AND lease_until > NOW()
            UNION ALL
            SELECT id FROM offhours_summary_tasks
            WHERE status = 'running' AND lease_until > NOW()
            UNION ALL
            SELECT id FROM creator_import_rows
            WHERE status = 'running' AND lease_until > NOW()
            UNION ALL
            SELECT id FROM creator_handle_jobs
            WHERE status = 'running' AND lease_until > NOW()
        ) AS active
        """
    ).fetchone()
    return bool(active and active["count"] < MAX_CONCURRENT_REQUESTS)


def throttle(delay_seconds: float) -> None:
    """Temporarily hold all queues after a recoverable provider 429."""

    try:
        delay = float(delay_seconds)
    except (TypeError, ValueError):
        return
    if not math.isfinite(delay):
        return
    delay = max(delay, 0.0)
    with connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_LOCK,))
        conn.execute(
            """
            UPDATE ai_admission_state
            SET not_before = GREATEST(COALESCE(not_before, NOW()), NOW() + (%s * INTERVAL '1 second')),
                pause_reason = %s,
                updated_at = NOW()
            WHERE id = TRUE AND paused_at IS NULL
            """,
            (delay, _TEMPORARY_RATE_LIMIT_MESSAGE),
        )


def observe_limits(telemetry: dict[str, Any]) -> None:
    """Hold shared admission when the provider reports an exhausted bucket."""
    delays = []
    for bucket in ("requests", "tokens"):
        if telemetry.get("rate_limit_remaining_" + bucket) != 0:
            continue
        reset = str(telemetry.get("rate_limit_reset_" + bucket) or "")
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", reset)
        seconds = sum(float(value) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit] for value, unit in parts)
        delays.append(seconds if parts and math.isfinite(seconds) and seconds > 0 else 60)
    if delays:
        throttle(max(delays))


def pause_for_quota() -> None:
    """Stop all queues until an owner explicitly resumes them."""

    with connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_LOCK,))
        conn.execute(
            """
            UPDATE ai_admission_state
            SET paused_at = COALESCE(paused_at, NOW()), not_before = NULL,
                pause_reason = %s, updated_at = NOW()
            WHERE id = TRUE
            """,
            (_QUOTA_PAUSE_MESSAGE,),
        )


def resume(conn: psycopg.Connection) -> None:
    """Clear an owner-acknowledged temporary or quota pause."""

    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_LOCK,))
    conn.execute(
        """
        UPDATE ai_admission_state
        SET paused_at = NULL, not_before = NULL, pause_reason = NULL, updated_at = NOW()
        WHERE id = TRUE
        """
    )


def status(conn: psycopg.Connection) -> dict[str, Any]:
    """Return the small owner-safe state needed by the AI settings page."""

    row = conn.execute(
        """
        SELECT paused_at, not_before > NOW() AS throttled, pause_reason, updated_at
        FROM ai_admission_state WHERE id = TRUE
        """
    ).fetchone()
    if not row:
        return {"state": "ready", "label": "可领取任务", "badge": "success", "message": ""}
    if row["paused_at"] is not None:
        return {
            "state": "paused",
            "label": "AI 队列已暂停",
            "badge": "danger",
            "message": _QUOTA_PAUSE_MESSAGE,
        }
    if row["throttled"]:
        return {
            "state": "throttled",
            "label": "等待服务端限速恢复",
            "badge": "warning",
            "message": _TEMPORARY_RATE_LIMIT_MESSAGE,
        }
    return {"state": "ready", "label": "可领取任务", "badge": "success", "message": ""}

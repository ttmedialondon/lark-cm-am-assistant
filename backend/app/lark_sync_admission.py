"""Small PostgreSQL admission gate for concurrent Lark sync requests."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from .db import connection

REQUEST_LEASE_SECONDS = 45
RATE_LIMIT_FALLBACK_SECONDS = 60


class LarkSyncAdmission:
    """Bound all worker Lark calls across processes without another queue service."""

    def __init__(self, *, max_inflight: int, requests_per_second: int) -> None:
        self.max_inflight = max_inflight
        self.slot_interval = timedelta(seconds=1 / requests_per_second)

    @contextmanager
    def request(self) -> Iterator[None]:
        lease_id = self._acquire()
        try:
            yield
        finally:
            self._release(lease_id)

    def pause(self, seconds: int | None) -> None:
        delay = RATE_LIMIT_FALLBACK_SECONDS if seconds is None else max(0, seconds)
        with connection() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE lark_sync_admission_state
                SET paused_until = GREATEST(
                        COALESCE(paused_until, '-infinity'::timestamptz),
                        clock_timestamp() + (%s * INTERVAL '1 second')
                    ),
                    last_rate_limited_at = clock_timestamp(),
                    rate_limited_count = rate_limited_count + 1,
                    updated_at = NOW()
                WHERE id
                """,
                (delay,),
            )

    def _acquire(self) -> uuid.UUID:
        while True:
            lease_id = uuid.uuid4()
            wait_seconds: float | None = None
            with connection() as conn, conn.transaction():
                conn.execute("DELETE FROM lark_sync_request_leases WHERE expires_at <= clock_timestamp()")
                state = conn.execute(
                    """
                    SELECT next_request_at, paused_until, clock_timestamp() AS now
                    FROM lark_sync_admission_state
                    WHERE id FOR UPDATE
                    """
                ).fetchone()
                if not state:
                    raise RuntimeError("Lark sync admission state is missing")
                active = conn.execute(
                    "SELECT COUNT(*) AS count FROM lark_sync_request_leases WHERE expires_at > clock_timestamp()"
                ).fetchone()
                now = _as_datetime(state["now"])
                not_before = max(
                    _as_datetime(state["next_request_at"]),
                    _as_datetime(state["paused_until"]) if state["paused_until"] else now,
                )
                if int(active["count"]) < self.max_inflight and not_before <= now:
                    conn.execute(
                        """
                        INSERT INTO lark_sync_request_leases (id, expires_at)
                        VALUES (%s, clock_timestamp() + (%s * INTERVAL '1 second'))
                        """,
                        (lease_id, REQUEST_LEASE_SECONDS),
                    )
                    conn.execute(
                        """
                        UPDATE lark_sync_admission_state
                        SET next_request_at = clock_timestamp() + (%s * INTERVAL '1 second'),
                            updated_at = NOW()
                        WHERE id
                        """,
                        (self.slot_interval.total_seconds(),),
                    )
                    return lease_id
                wait_seconds = max(0.02, (not_before - now).total_seconds())
                if int(active["count"]) >= self.max_inflight:
                    wait_seconds = min(wait_seconds, 0.05) if wait_seconds else 0.05
            time.sleep(wait_seconds)

    @staticmethod
    def _release(lease_id: uuid.UUID) -> None:
        with connection() as conn, conn.transaction():
            conn.execute("DELETE FROM lark_sync_request_leases WHERE id = %s", (lease_id,))


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError("Lark sync admission state contains an invalid timestamp")
    return value

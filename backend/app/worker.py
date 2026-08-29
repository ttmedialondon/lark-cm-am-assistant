"""Independent PostgreSQL-backed worker for discovery, backfill, and incremental statistics."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .config import Settings, get_settings
from .crypto import TokenCipher
from .db import connection
from .lark_api import LarkApiError, LarkClient, TokenBundle

LOGGER = logging.getLogger("lark_message_stats.worker")
LEASE_MINUTES = 10


class ReauthorizationRequired(RuntimeError):
    """The user must complete OAuth again before the worker can proceed."""


@dataclass(frozen=True)
class SyncJob:
    id: uuid.UUID
    authorization_id: uuid.UUID
    mode: str
    attempts: int


@dataclass(frozen=True)
class MemberCountMeasurement:
    """A non-sensitive aggregate returned for one chat."""

    count: int | None
    status: str
    checked_at: datetime | None


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if not settings.app_secret_configured:
        LOGGER.warning("Lark App Secret is not configured; worker is waiting for administrator input")

    while True:
        try:
            _recover_expired_leases()
            if not settings.app_secret_configured:
                time.sleep(settings.worker_idle_seconds)
                continue
            job = _claim_job()
            if not job:
                time.sleep(settings.worker_idle_seconds)
                continue
            _execute_job(job, settings)
        except KeyboardInterrupt:
            raise
        except Exception:  # Unexpected failures must not terminate the systemd worker loop.
            LOGGER.exception("worker loop failed without exposing user data")
            time.sleep(settings.worker_idle_seconds)


def _recover_expired_leases() -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'queued', scheduled_at = NOW(), lease_until = NULL,
                last_error = 'A worker lease expired; the job was safely resumed.', updated_at = NOW()
            WHERE status = 'running' AND lease_until < NOW()
            """
        )


def _claim_job() -> SyncJob | None:
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """
            SELECT id, authorization_id, mode, attempts
            FROM sync_jobs
            WHERE status = 'queued' AND scheduled_at <= NOW()
            ORDER BY scheduled_at, created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'running', started_at = COALESCE(started_at, NOW()),
                attempts = attempts + 1,
                lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s
            """,
            (row["id"],),
        )
        return SyncJob(row["id"], row["authorization_id"], row["mode"], int(row["attempts"]) + 1)


def _execute_job(job: SyncJob, settings: Settings) -> None:
    try:
        authorization = _authorization(job.authorization_id)
        if not authorization or authorization["status"] != "active":
            _finish_job(job.id, "cancelled", "Authorization is no longer active.")
            return

        client = LarkClient(settings)
        access_token = _usable_access_token(job.authorization_id, authorization, client, settings)
        discovered = _discover_groups(job, authorization, access_token, client, settings)
        _set_job_discovered(job.id, discovered)

        if job.mode == "backfill":
            complete = _run_backfill(job, authorization, access_token, client, settings)
        else:
            complete = _run_incremental(job, authorization, access_token, client, settings)

        if complete:
            _finish_and_schedule_incremental(job)
        else:
            _requeue(job.id, delay_seconds=0, detail="Continuing a paginated sync safely from its saved cursor.")
    except ReauthorizationRequired:
        _mark_attention(job, "Lark authorization must be renewed.")
    except LarkApiError as exc:
        if exc.likely_auth_problem:
            _mark_attention(job, "Lark authorization or permission needs attention.")
        elif exc.retryable:
            delay = exc.retry_after or min(60 * (2 ** min(job.attempts, 4)), 900)
            _requeue(
                job.id,
                delay_seconds=delay,
                detail="Lark is temporarily unavailable; retry scheduled.",
                code=exc.lark_code,
            )
        else:
            _finish_job(job.id, "failed", "A Lark API request could not be completed.", exc.lark_code)
    except (psycopg.Error, ValueError):
        _requeue(
            job.id,
            delay_seconds=min(60 * (2 ** min(job.attempts, 4)), 900),
            detail="Temporary service issue; retry scheduled.",
        )


def _authorization(authorization_id: uuid.UUID) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            """
            SELECT id, open_id, status, access_token_ciphertext, refresh_token_ciphertext,
                   access_token_expires_at, refresh_token_expires_at
            FROM authorizations WHERE id = %s
            """,
            (authorization_id,),
        ).fetchone()


def _usable_access_token(
    authorization_id: uuid.UUID, authorization: dict[str, Any], client: LarkClient, settings: Settings
) -> str:
    cipher = TokenCipher(settings.token_encryption_key)
    now = datetime.now(UTC)
    expires_at = authorization.get("access_token_expires_at")
    if expires_at and expires_at > now + timedelta(minutes=2):
        return cipher.decrypt(authorization["access_token_ciphertext"])

    # Lark refresh tokens are single-use. Lock this authorization row while replacing both tokens atomically.
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """
            SELECT access_token_ciphertext, refresh_token_ciphertext,
                   access_token_expires_at, refresh_token_expires_at
            FROM authorizations WHERE id = %s FOR UPDATE
            """,
            (authorization_id,),
        ).fetchone()
        if not row or not row["refresh_token_ciphertext"]:
            raise ReauthorizationRequired
        if row["access_token_expires_at"] and row["access_token_expires_at"] > now + timedelta(minutes=2):
            return cipher.decrypt(row["access_token_ciphertext"])
        if row["refresh_token_expires_at"] and row["refresh_token_expires_at"] <= now:
            raise ReauthorizationRequired

        bundle = client.refresh_token(cipher.decrypt(row["refresh_token_ciphertext"]))
        if not bundle.refresh_token:
            raise ReauthorizationRequired
        _store_refreshed_tokens(conn, authorization_id, bundle, cipher)
        return bundle.access_token


def _store_refreshed_tokens(
    conn: psycopg.Connection, authorization_id: uuid.UUID, bundle: TokenBundle, cipher: TokenCipher
) -> None:
    conn.execute(
        """
        UPDATE authorizations
        SET access_token_ciphertext = %s, refresh_token_ciphertext = %s,
            access_token_expires_at = %s, refresh_token_expires_at = %s,
            status = 'active', last_error = NULL, updated_at = NOW()
        WHERE id = %s
        """,
        (
            cipher.encrypt(bundle.access_token),
            cipher.encrypt(bundle.refresh_token or ""),
            _expiry(bundle.expires_in),
            _expiry(bundle.refresh_expires_in),
            authorization_id,
        ),
    )


def _discover_groups(
    job: SyncJob,
    authorization: dict[str, Any],
    access_token: str,
    client: LarkClient,
    settings: Settings,
) -> int:
    """Discover visible groups and periodically refresh only their member totals.

    Group discovery and message ingestion must stay independent: an unavailable
    member-total API must never make an otherwise readable group look empty or
    prevent its message history from being indexed.
    """

    page_token: str | None = None
    discovered = 0
    seen_at = datetime.now(UTC)
    last_member_count_checks = _member_count_check_times(authorization["id"])
    while True:
        _renew_lease(job.id)
        data = client.list_group_chats(access_token, page_token)
        rows: list[tuple[str, str | None, bool, str, str, MemberCountMeasurement | None]] = []
        for chat in _items(data):
            chat_id = _text(chat.get("chat_id"))
            if not chat_id:
                continue
            measurement: MemberCountMeasurement | None = None
            if _member_count_refresh_due(
                last_member_count_checks.get(chat_id), seen_at, settings.member_count_refresh_seconds
            ):
                _renew_lease(job.id)
                measurement = _read_member_count(client, access_token, chat_id)
                if measurement.checked_at:
                    last_member_count_checks[chat_id] = measurement.checked_at
            rows.append(
                (
                    chat_id,
                    _text(chat.get("name")),
                    _bool(chat.get("external")),
                    _text(chat.get("chat_status")) or "normal",
                    _text(chat.get("chat_mode")) or "group",
                    measurement,
                )
            )

        with connection() as conn, conn.transaction():
            for chat_id, name, external, chat_status, chat_mode, measurement in rows:
                member_count = measurement.count if measurement else None
                member_count_status = measurement.status if measurement else "pending"
                member_count_checked_at = measurement.checked_at if measurement else None
                conn.execute(
                    """
                    INSERT INTO user_chats (
                        authorization_id, chat_id, name, external, chat_status, chat_mode,
                        is_visible, last_seen_at,
                        member_count, member_count_status, member_count_checked_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s)
                    ON CONFLICT (authorization_id, chat_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        external = EXCLUDED.external,
                        chat_status = EXCLUDED.chat_status,
                        chat_mode = EXCLUDED.chat_mode,
                        is_visible = TRUE,
                        last_seen_at = EXCLUDED.last_seen_at,
                        member_count = CASE
                            WHEN EXCLUDED.member_count_checked_at IS NULL THEN user_chats.member_count
                            ELSE EXCLUDED.member_count
                        END,
                        member_count_status = CASE
                            WHEN EXCLUDED.member_count_checked_at IS NULL THEN user_chats.member_count_status
                            ELSE EXCLUDED.member_count_status
                        END,
                        member_count_checked_at = COALESCE(
                            EXCLUDED.member_count_checked_at, user_chats.member_count_checked_at
                        )
                    """,
                    (
                        authorization["id"],
                        chat_id,
                        name,
                        external,
                        chat_status,
                        chat_mode,
                        seen_at,
                        member_count,
                        member_count_status,
                        member_count_checked_at,
                    ),
                )
                discovered += 1
        if not _has_more(data):
            break
        page_token = _text(data.get("page_token"))
        if not page_token:
            raise LarkApiError(502, None, "Lark chat pagination did not return a continuation token")

    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE user_chats SET is_visible = FALSE
            WHERE authorization_id = %s AND last_seen_at < %s
            """,
            (authorization["id"], seen_at),
        )
    return discovered


def _member_count_check_times(authorization_id: uuid.UUID) -> dict[str, datetime | None]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, member_count_checked_at
            FROM user_chats
            WHERE authorization_id = %s
            """,
            (authorization_id,),
        ).fetchall()
    return {row["chat_id"]: row["member_count_checked_at"] for row in rows}


def _member_count_refresh_due(
    last_checked_at: datetime | None, now: datetime, refresh_seconds: int
) -> bool:
    return last_checked_at is None or last_checked_at <= now - timedelta(seconds=refresh_seconds)


def _read_member_count(client: LarkClient, access_token: str, chat_id: str) -> MemberCountMeasurement:
    checked_at = datetime.now(UTC)
    try:
        total = client.chat_member_total(access_token, chat_id).user_total
    except LarkApiError as exc:
        # This is secondary metadata. Preserve the group and primary message
        # sync even when a tenant policy or a missing scope denies this call.
        status = "pending" if exc.retryable else "unavailable"
        LOGGER.info("member-count metadata unavailable: status=%s code=%s", exc.status_code, exc.lark_code)
        return MemberCountMeasurement(count=None, status=status, checked_at=checked_at)
    if total is None:
        LOGGER.info("member-count metadata response omitted a usable user total")
        return MemberCountMeasurement(count=None, status="unavailable", checked_at=checked_at)
    return MemberCountMeasurement(count=total, status="succeeded", checked_at=checked_at)


def _run_backfill(
    job: SyncJob, authorization: dict[str, Any], access_token: str, client: LarkClient, settings: Settings
) -> bool:
    pages = 0
    while pages < settings.max_pages_per_job:
        chat = _next_backfill_chat(authorization["id"])
        if not chat:
            return True
        now = datetime.now(UTC)
        start_at = chat["backfill_window_start_at"]
        end_at = chat["backfill_window_end_at"]
        if not end_at:
            end_at = now
            start_at = now - timedelta(days=settings.sync_lookback_days) if settings.sync_lookback_days else None
            _start_backfill_window(authorization["id"], chat["chat_id"], start_at, end_at)

        try:
            data = client.list_chat_messages(
                access_token,
                chat["chat_id"],
                page_token=chat["backfill_page_token"],
                start_at=start_at,
                end_at=end_at,
                ascending=True,
            )
        except LarkApiError as exc:
            if exc.retryable or exc.likely_auth_problem:
                raise
            _mark_chat_not_readable(authorization["id"], chat["chat_id"])
            continue

        next_page_token = _continuation_token(data)

        inserted, own_inserted = _record_page(
            job,
            authorization,
            chat["chat_id"],
            _items(data),
            next_page_token=next_page_token,
            backfill=True,
        )
        pages += 1
        _add_job_progress(job.id, pages=1, messages=inserted, own_messages=own_inserted)
    return False


def _run_incremental(
    job: SyncJob, authorization: dict[str, Any], access_token: str, client: LarkClient, settings: Settings
) -> bool:
    pages = 0
    while pages < settings.max_pages_per_job:
        chat = _next_incremental_chat(authorization["id"], job.id)
        if not chat:
            return True
        start_at = chat["incremental_window_start_at"]
        end_at = chat["incremental_window_end_at"]
        if not end_at:
            end_at = datetime.now(UTC)
            last_sync = chat["last_incremental_at"] or chat["last_backfill_at"]
            if last_sync:
                start_at = last_sync - timedelta(seconds=settings.sync_overlap_seconds)
            else:
                start_at = end_at - timedelta(days=settings.sync_lookback_days or 1)
        if chat["incremental_job_id"] != job.id:
            _start_incremental_window(authorization["id"], chat["chat_id"], job.id, start_at, end_at)

        try:
            data = client.list_chat_messages(
                access_token,
                chat["chat_id"],
                page_token=chat["incremental_page_token"],
                start_at=start_at,
                end_at=end_at,
                ascending=True,
            )
        except LarkApiError as exc:
            if exc.retryable or exc.likely_auth_problem:
                raise
            _mark_chat_not_readable(authorization["id"], chat["chat_id"])
            continue

        next_page_token = _continuation_token(data)

        inserted, own_inserted = _record_page(
            job,
            authorization,
            chat["chat_id"],
            _items(data),
            next_page_token=next_page_token,
            backfill=False,
        )
        pages += 1
        _add_job_progress(job.id, pages=1, messages=inserted, own_messages=own_inserted)
    return False


def _next_backfill_chat(authorization_id: uuid.UUID) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            """
            SELECT * FROM user_chats
            WHERE authorization_id = %s AND sync_enabled AND is_visible
              AND COALESCE(chat_status, 'normal') = 'normal' AND NOT backfill_complete
              AND sync_status <> 'not_readable'
            ORDER BY discovered_at, chat_id LIMIT 1
            """,
            (authorization_id,),
        ).fetchone()


def _next_incremental_chat(authorization_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            """
            SELECT * FROM user_chats
            WHERE authorization_id = %s AND sync_enabled AND is_visible
              AND COALESCE(chat_status, 'normal') = 'normal' AND backfill_complete
              AND sync_status <> 'not_readable'
              AND (
                    (incremental_job_id = %s AND incremental_page_token IS NOT NULL)
                 OR incremental_job_id IS DISTINCT FROM %s
              )
            ORDER BY
                CASE WHEN incremental_job_id = %s AND incremental_page_token IS NOT NULL THEN 0 ELSE 1 END,
                COALESCE(last_incremental_at, last_backfill_at), chat_id
            LIMIT 1
            """,
            (authorization_id, job_id, job_id, job_id),
        ).fetchone()


def _start_backfill_window(
    authorization_id: uuid.UUID, chat_id: str, start_at: datetime | None, end_at: datetime
) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE user_chats
            SET backfill_started_at = COALESCE(backfill_started_at, NOW()),
                backfill_window_start_at = COALESCE(backfill_window_start_at, %s),
                backfill_window_end_at = COALESCE(backfill_window_end_at, %s),
                sync_status = 'syncing', last_error = NULL
            WHERE authorization_id = %s AND chat_id = %s
            """,
            (start_at, end_at, authorization_id, chat_id),
        )


def _start_incremental_window(
    authorization_id: uuid.UUID, chat_id: str, job_id: uuid.UUID, start_at: datetime, end_at: datetime
) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE user_chats
            SET incremental_job_id = %s,
                incremental_window_start_at = COALESCE(incremental_window_start_at, %s),
                incremental_window_end_at = COALESCE(incremental_window_end_at, %s),
                sync_status = 'syncing', last_error = NULL
            WHERE authorization_id = %s AND chat_id = %s
            """,
            (job_id, start_at, end_at, authorization_id, chat_id),
        )


def _record_page(
    job: SyncJob,
    authorization: dict[str, Any],
    chat_id: str,
    messages: list[dict[str, Any]],
    *,
    next_page_token: str | None,
    backfill: bool,
) -> tuple[int, int]:
    inserted = 0
    own_inserted = 0
    with connection() as conn, conn.transaction():
        for message in messages:
            message_id = _text(message.get("message_id"))
            if not message_id:
                continue
            is_own = _is_author_message(message, authorization["open_id"])
            is_deleted = _bool(message.get("deleted"))
            row = conn.execute(
                """
                INSERT INTO message_ledger (
                    authorization_id, chat_id, message_id, message_create_time, sent_by_author, is_deleted
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (authorization_id, chat_id, message_id) DO NOTHING
                RETURNING sent_by_author
                """,
                (
                    authorization["id"],
                    chat_id,
                    message_id,
                    _lark_time(message.get("create_time")),
                    is_own,
                    is_deleted,
                ),
            ).fetchone()
            if row:
                inserted += 1
                own_inserted += int(bool(row["sent_by_author"]))
            else:
                conn.execute(
                    """
                    UPDATE message_ledger
                    SET is_deleted = %s, indexed_at = NOW()
                    WHERE authorization_id = %s AND chat_id = %s AND message_id = %s
                      AND is_deleted IS DISTINCT FROM %s
                    """,
                    (is_deleted, authorization["id"], chat_id, message_id, is_deleted),
                )

        if backfill:
            conn.execute(
                """
                UPDATE user_chats
                SET backfill_page_token = %s,
                    backfill_complete = %s,
                    last_backfill_at = CASE WHEN %s THEN NOW() ELSE last_backfill_at END,
                    sync_status = CASE WHEN %s THEN 'succeeded' ELSE 'syncing' END,
                    last_error = NULL
                WHERE authorization_id = %s AND chat_id = %s
                """,
                (
                    next_page_token,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    authorization["id"],
                    chat_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE user_chats
                SET incremental_page_token = %s,
                    incremental_window_start_at = CASE WHEN %s THEN NULL ELSE incremental_window_start_at END,
                    incremental_window_end_at = CASE WHEN %s THEN NULL ELSE incremental_window_end_at END,
                    last_incremental_at = CASE WHEN %s THEN NOW() ELSE last_incremental_at END,
                    sync_status = CASE WHEN %s THEN 'succeeded' ELSE 'syncing' END,
                    last_error = NULL
                WHERE authorization_id = %s AND chat_id = %s
                """,
                (
                    next_page_token,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    authorization["id"],
                    chat_id,
                ),
            )
    return inserted, own_inserted


def _mark_chat_not_readable(authorization_id: uuid.UUID, chat_id: str) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE user_chats
            SET sync_status = 'not_readable', backfill_complete = TRUE,
                incremental_page_token = NULL, last_error = 'This group is not readable with the current authorization.'
            WHERE authorization_id = %s AND chat_id = %s
            """,
            (authorization_id, chat_id),
        )


def _renew_lease(job_id: uuid.UUID) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE sync_jobs SET lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW() WHERE id = %s",
            (job_id,),
        )


def _set_job_discovered(job_id: uuid.UUID, discovered: int) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE sync_jobs SET chats_discovered = %s, updated_at = NOW() WHERE id = %s",
            (discovered, job_id),
        )


def _add_job_progress(job_id: uuid.UUID, *, pages: int, messages: int, own_messages: int) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET pages_processed = pages_processed + %s,
                messages_indexed = messages_indexed + %s,
                own_messages_indexed = own_messages_indexed + %s,
                lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s
            """,
            (pages, messages, own_messages, job_id),
        )


def _finish_and_schedule_incremental(job: SyncJob) -> None:
    settings = get_settings()
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'succeeded', finished_at = NOW(), lease_until = NULL, last_error = NULL, updated_at = NOW()
            WHERE id = %s
            """,
            (job.id,),
        )
        conn.execute(
            """
            INSERT INTO sync_jobs (id, authorization_id, mode, status, scheduled_at)
            VALUES (%s, %s, 'incremental', 'queued', NOW() + (%s * INTERVAL '1 second'))
            ON CONFLICT (authorization_id) WHERE status IN ('queued', 'running') DO NOTHING
            """,
            (uuid.uuid4(), job.authorization_id, settings.sync_interval_seconds),
        )


def _requeue(job_id: uuid.UUID, *, delay_seconds: int, detail: str, code: int | None = None) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'queued', scheduled_at = NOW() + (%s * INTERVAL '1 second'),
                lease_until = NULL, last_error_code = %s, last_error = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (delay_seconds, code, detail, job_id),
        )


def _finish_job(job_id: uuid.UUID, status: str, detail: str, code: int | None = None) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = %s, finished_at = NOW(), lease_until = NULL,
                last_error_code = %s, last_error = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (status, code, detail, job_id),
        )


def _mark_attention(job: SyncJob, detail: str) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE authorizations
            SET status = 'attention_required', last_error = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (detail, job.authorization_id),
        )
        conn.execute(
            """
            UPDATE sync_jobs SET status = 'attention_required', finished_at = NOW(), lease_until = NULL,
                last_error = %s, updated_at = NOW() WHERE id = %s
            """,
            (detail, job.id),
        )


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = data.get("items")
    return [item for item in candidate if isinstance(item, dict)] if isinstance(candidate, list) else []


def _has_more(data: dict[str, Any]) -> bool:
    value = data.get("has_more")
    return value is True or value == "true"


def _continuation_token(data: dict[str, Any]) -> str | None:
    if not _has_more(data):
        return None
    token = _text(data.get("page_token"))
    if not token:
        raise LarkApiError(502, None, "Lark message pagination did not return a continuation token")
    return token


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bool(value: object) -> bool:
    return value is True or value == 1 or value == "true"


def _is_author_message(message: dict[str, Any], open_id: str) -> bool:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return False
    return (
        sender.get("sender_type") == "user"
        and sender.get("id_type") == "open_id"
        and sender.get("id") == open_id
    )


def _lark_time(value: object) -> datetime | None:
    try:
        milliseconds = int(value)  # Lark's create_time is a millisecond string.
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _expiry(seconds: int | None) -> datetime | None:
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds else None


if __name__ == "__main__":
    run_forever()

"""Independent PostgreSQL-backed worker for discovery, backfill, and incremental statistics."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from . import (
    ai_dispatch,
    assistant,
    creator_broadcast_delivery,
    creator_group_matching,
    creator_handle_matching,
    creator_importance,
    creator_imports,
    creator_responsibility_matching,
    identity_profiles,
    offhours_summary,
    organization_sync,
    summary_replies,
)
from .config import Settings, get_settings
from .crypto import TokenCipher, message_body_cipher, message_body_fingerprint
from .db import connection, lock_sync_material
from .identity_names import format_display_name, update_identity_names
from .lark_api import LarkApiError, LarkClient, TokenBundle
from .lark_sync_admission import LarkSyncAdmission

LOGGER = logging.getLogger("lark_message_stats.worker")
LEASE_MINUTES = 10


class ReauthorizationRequired(RuntimeError):
    """The user must complete OAuth again before the worker can proceed."""


class StaleSyncJob(RuntimeError):
    """A reauthorization replaced this job's discovery or page generation."""


@dataclass(frozen=True)
class SyncJob:
    id: uuid.UUID
    authorization_id: uuid.UUID
    mode: str
    attempts: int
    discovery_status: str
    discovery_generation: int


@dataclass(frozen=True)
class PageClaim:
    """One durable, user-scoped Lark message-page reservation."""

    job: SyncJob
    chat_id: str
    mode: str
    generation: int
    claim_id: uuid.UUID
    expected_page_token: str | None
    start_at: datetime | None
    end_at: datetime
    author_open_id: str
    body_consent_at: datetime | None


@dataclass(frozen=True)
class PageCommit:
    committed: bool
    messages: int = 0
    own_messages: int = 0


@dataclass(frozen=True)
class MemberSnapshotMeasurement:
    """The latest roster fact and its safe-to-display aggregate."""

    count: int | None
    count_status: str
    snapshot_status: str
    checked_at: datetime | None
    members: tuple[dict[str, Any], ...] = ()
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class MemberSnapshotClaim:
    authorization_id: uuid.UUID
    chat_id: str
    generation: int
    claim_id: uuid.UUID


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx otherwise logs full request URLs, including Lark chat IDs, at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.app_secret_configured:
        LOGGER.warning("Lark App Secret is not configured; worker is waiting for administrator input")

    next_ai_queue = 0
    next_ai_check = 0.0
    next_organization_check = 0.0
    ai_executor = ThreadPoolExecutor(max_workers=ai_dispatch.MAX_CONCURRENT_REQUESTS, thread_name_prefix="openai")
    ai_futures: set[Future[Any]] = set()
    broadcast_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lark-broadcast")
    broadcast_futures: set[Future[Any]] = set()
    next_broadcast_check = 0.0
    next_broadcast_recovery = 0.0
    preview_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="creator-preview")
    preview_future: Future[tuple[bool, int]] | None = None
    next_preview_check = 0.0
    next_preview_queue = 0
    avatar_executor = ThreadPoolExecutor(max_workers=identity_profiles.MAX_WORKERS, thread_name_prefix="lark-avatar")
    avatar_futures: set[Future[Any]] = set()
    # PostgreSQL owns the cross-process limit; this executor only provides the
    # local page fetches.  Writes remain on the main thread below.
    lark_executor = ThreadPoolExecutor(
        max_workers=settings.lark_sync_max_inflight + settings.lark_member_snapshot_max_inflight,
        thread_name_prefix="lark-sync",
    )
    discovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lark-discovery")
    discovery_future: Future[bool] | None = None
    next_discovery_check = 0.0
    page_futures: dict[Future[dict[str, Any]], PageClaim] = {}
    member_futures: dict[Future[MemberSnapshotMeasurement], MemberSnapshotClaim] = {}
    admission = LarkSyncAdmission(
        max_inflight=settings.lark_sync_max_inflight,
        requests_per_second=settings.lark_sync_requests_per_second,
    )
    http_client = httpx.Client(
        timeout=httpx.Timeout(20.0, connect=8.0),
        limits=httpx.Limits(
            max_connections=settings.lark_sync_max_inflight + settings.lark_member_snapshot_max_inflight,
            max_keepalive_connections=settings.lark_sync_max_inflight + settings.lark_member_snapshot_max_inflight,
        ),
    )
    client = LarkClient(settings, http_client=http_client, admission=admission)
    try:
        while True:
            try:
                _reap_page_futures(page_futures, settings)
                _reap_member_futures(member_futures)
                _recover_expired_leases()
                next_ai_queue, next_ai_check = _schedule_ai(
                    settings, ai_executor, ai_futures, next_ai_queue, next_ai_check,
                )
                preview_future, next_preview_check, next_preview_queue = _schedule_creator_preview(
                    settings, preview_executor, preview_future, next_preview_check, next_preview_queue,
                )
                if settings.app_secret_configured and time.monotonic() >= next_organization_check:
                    _run_organization_sync(settings, client)
                    # The database keeps the real daily cadence. This local bound
                    # avoids a query on every three-second idle worker loop.
                    next_organization_check = time.monotonic() + min(
                        getattr(settings, "organization_sync_interval_seconds", 21_600), 300
                    )
                if settings.app_secret_configured:
                    if time.monotonic() >= next_broadcast_recovery:
                        creator_broadcast_delivery.recover()
                        next_broadcast_recovery = time.monotonic() + 60
                    next_broadcast_check = _schedule_broadcast(
                        settings, broadcast_executor, broadcast_futures, next_broadcast_check,
                    )
                    _finish_completed_jobs(page_futures)
                    _fill_lark_slots(settings, client, lark_executor, page_futures)
                    discovery_future, next_discovery_check = _schedule_discovery(
                        settings, client, discovery_executor, discovery_future, next_discovery_check,
                    )
                    _finish_completed_jobs(page_futures)
                    _fill_lark_slots(settings, client, lark_executor, page_futures)
                    _schedule_member_snapshot(settings, client, lark_executor, page_futures, member_futures)
                    identity_profiles.fill_slots(settings, client, avatar_executor, avatar_futures)
                time.sleep(
                    0.05 if page_futures or member_futures or avatar_futures or preview_future or broadcast_futures
                    or discovery_future
                    else settings.worker_idle_seconds
                )
            except KeyboardInterrupt:
                raise
            except Exception:  # Unexpected failures must not terminate the systemd worker loop.
                LOGGER.exception("worker loop failed without exposing user data")
                time.sleep(settings.worker_idle_seconds)
    finally:
        # Leases preserve in-flight work if systemd terminates us during a call.
        ai_executor.shutdown(wait=False, cancel_futures=False)
        broadcast_executor.shutdown(wait=False, cancel_futures=False)
        preview_executor.shutdown(wait=False, cancel_futures=False)
        discovery_executor.shutdown(wait=False, cancel_futures=False)
        lark_executor.shutdown(wait=False, cancel_futures=False)
        avatar_executor.shutdown(wait=False, cancel_futures=False)
        http_client.close()


def _run_organization_sync(settings: Settings, client: LarkClient) -> None:
    """Keep an optional Trust Party failure separate from message collection."""

    try:
        organization_sync.sync_if_due(settings, client)
    except Exception:
        LOGGER.exception("organization sync failed without exposing organization data")


def _schedule_broadcast(settings, executor, futures, next_check):
    """Separate sending slots; the durable claim serializes each sender account."""
    completed = False
    for future in tuple(futures):
        if not future.done():
            continue
        futures.remove(future)
        completed = True
        try:
            future.result()
        except Exception:
            LOGGER.exception("broadcast delivery failed without exposing message data")
    if completed:
        next_check = 0.0
    if time.monotonic() < next_check:
        return next_check
    while len(futures) < 2:
        job = creator_broadcast_delivery.claim_next()
        if not job:
            return time.monotonic() + max(settings.worker_idle_seconds, 0.05)
        futures.add(executor.submit(creator_broadcast_delivery.run_claimed, job, settings))
    return 0.0


def _dispatch_ai_turn(
    settings: Settings,
    executor: ThreadPoolExecutor,
    futures: set[Future[Any]],
    next_queue: int,
) -> tuple[bool, int]:
    """Claim one fair AI task and submit it without blocking Lark sync."""

    if len(futures) >= ai_dispatch.MAX_CONCURRENT_REQUESTS:
        return False, next_queue
    queues = (
        (assistant.claim, assistant.run),
        (
            offhours_summary.claim_next_summary,
            offhours_summary.run_claimed_summary,
        ),
        (creator_imports.claim_next_import, creator_imports.run_claimed_import),
        (summary_replies.claim, summary_replies.run),
        (creator_handle_matching.claim, creator_handle_matching.run),
        (creator_responsibility_matching.claim, creator_responsibility_matching.run),
        (creator_group_matching.claim, creator_group_matching.run),
        (creator_importance.claim, creator_importance.run),
    )
    for offset in range(len(queues)):
        index = (next_queue + offset) % len(queues)
        claim, runner = queues[index]
        try:
            job = claim()
        except Exception:
            LOGGER.exception("AI queue claim failed without exposing user data")
            continue
        if not job:
            continue
        futures.add(executor.submit(runner, job, settings))
        return True, (index + 1) % len(queues)
    return False, next_queue


def _fill_ai_slots(
    settings: Settings,
    executor: ThreadPoolExecutor,
    futures: set[Future[Any]],
    next_queue: int,
) -> int:
    """Fill available AI slots before returning immediately to Lark sync."""

    while len(futures) < ai_dispatch.MAX_CONCURRENT_REQUESTS:
        submitted, next_queue = _dispatch_ai_turn(settings, executor, futures, next_queue)
        if not submitted:
            break
    return next_queue


def _schedule_ai(settings, executor, futures, next_queue, next_check):
    """Empty AI queues follow the idle cadence, not the busy Lark 50ms loop."""
    previous = len(futures)
    _reap_ai_futures(futures)
    if len(futures) < previous:
        next_check = 0.0  # Completion immediately frees a slot for chained work.
    if time.monotonic() < next_check:
        return next_queue, next_check
    previous = len(futures)
    next_queue = _fill_ai_slots(settings, executor, futures, next_queue)
    next_check = (time.monotonic() + max(settings.worker_idle_seconds, 0.05)
                  if len(futures) == previous and len(futures) < ai_dispatch.MAX_CONCURRENT_REQUESTS else 0.0)
    return next_queue, next_check


def _prepare_creator_preview(settings: Settings, next_queue: int) -> tuple[bool, int]:
    """Give each local preparation queue a turn; an idle queue does not delay the other."""

    # Retired handle scanning is not produced or dispatched; historical review stays available.
    queues = (assistant.prepare, creator_imports.prepare_next_import,
              offhours_summary.prepare_next_summary,
              creator_handle_matching.prepare_next,
              creator_responsibility_matching.prepare_next,
              creator_group_matching.prepare_next,
              creator_importance.prepare_next)
    for offset in range(len(queues)):
        index = (next_queue + offset) % len(queues)
        try:
            if queues[index](settings):
                return True, (index + 1) % len(queues)
        except Exception:
            LOGGER.exception("creator preview failed without exposing user data")
    return False, next_queue


def _schedule_creator_preview(
    settings: Settings,
    executor: ThreadPoolExecutor,
    future: Future[tuple[bool, int]] | None,
    next_check: float,
    next_queue: int,
) -> tuple[Future[tuple[bool, int]] | None, float, int]:
    """Prepare one person's local evidence without using an OpenAI slot or blocking sync."""

    if future is not None:
        if not future.done():
            return future, next_check, next_queue
        progressed = False
        try:
            progressed, next_queue = future.result()
        except Exception:
            LOGGER.exception("creator preview failed without exposing user data")
        future = None
        if not progressed:
            next_check = time.monotonic() + max(settings.worker_idle_seconds, 1.0)
    if time.monotonic() >= next_check:
        future = executor.submit(_prepare_creator_preview, settings, next_queue)
    return future, next_check, next_queue


def _reap_ai_futures(futures: set[Future[Any]]) -> None:
    """Release finished slots and keep a worker-thread exception observable."""

    for future in tuple(futures):
        if not future.done():
            continue
        futures.remove(future)
        try:
            future.result()
        except Exception:
            LOGGER.exception("AI task runner failed without exposing user data")


def _recover_expired_leases() -> None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        expired_jobs = conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'queued', scheduled_at = NOW(), lease_until = NULL,
                attempts = attempts + 1,
                discovery_status = CASE
                    WHEN discovery_status = 'running' THEN 'pending'
                    ELSE discovery_status
                END,
                discovery_generation = CASE
                    WHEN discovery_status = 'running' THEN discovery_generation + 1
                    ELSE discovery_generation
                END,
                discovery_completed_at = CASE
                    WHEN discovery_status = 'running' THEN NULL
                    ELSE discovery_completed_at
                END,
                last_error_code = NULL,
                last_http_status = NULL,
                last_error = 'A worker lease expired; the job was safely resumed.', updated_at = NOW()
            WHERE status = 'running' AND lease_until < NOW()
            RETURNING id
            """
        ).fetchall()
        if expired_jobs:
            conn.execute(
                """
                UPDATE authorization_chats
                SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                    page_claimed_at = NULL, page_lease_until = NULL,
                    sync_status = CASE WHEN sync_status = 'syncing' AND backfill_complete
                        THEN 'succeeded'
                        WHEN sync_status = 'syncing' THEN 'pending'
                        ELSE sync_status
                    END
                WHERE page_claim_job_id = ANY(%s)
                """,
                ([row["id"] for row in expired_jobs],),
            )
        conn.execute(
            """
            UPDATE authorization_chats
            SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                page_claimed_at = NULL, page_lease_until = NULL,
                sync_status = CASE WHEN sync_status = 'syncing' AND backfill_complete
                    THEN 'succeeded'
                    WHEN sync_status = 'syncing' THEN 'pending'
                    ELSE sync_status
                END
            WHERE page_claim_id IS NOT NULL AND page_lease_until < NOW()
            """
        )
        conn.execute(
            """
            UPDATE authorization_chats
            SET member_snapshot_claim_id = NULL, member_snapshot_claimed_at = NULL,
                member_snapshot_lease_until = NULL
            WHERE member_snapshot_claim_id IS NOT NULL AND member_snapshot_lease_until < NOW()
            """
        )


def _claim_job() -> SyncJob | None:
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """
            SELECT id, authorization_id, mode, attempts, discovery_status, discovery_generation
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
                lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s
            """,
            (row["id"],),
        )
        return SyncJob(
            row["id"],
            row["authorization_id"],
            row["mode"],
            int(row["attempts"]),
            row["discovery_status"],
            int(row["discovery_generation"]),
        )


def _running_jobs() -> list[SyncJob]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, authorization_id, mode, attempts, discovery_status, discovery_generation
            FROM sync_jobs
            WHERE status = 'running' AND lease_until > NOW()
            ORDER BY COALESCE(last_progress_at, started_at, created_at), created_at
            """
        ).fetchall()
    return [
        SyncJob(
            row["id"],
            row["authorization_id"],
            row["mode"],
            int(row["attempts"]),
            row["discovery_status"],
            int(row["discovery_generation"]),
        )
        for row in rows
    ]


def _schedule_discovery(settings, client, executor, future, next_check):
    """Discover one account without blocking page commits, AI or message delivery."""

    if future is not None:
        if not future.done():
            return future, next_check
        try:
            progressed = future.result()
        except Exception:
            LOGGER.exception("Lark discovery failed without exposing user data")
            progressed = False
        future = None
        next_check = (0.0 if progressed else time.monotonic() + settings.worker_idle_seconds)
    if time.monotonic() >= next_check:
        future = executor.submit(_start_pending_jobs, settings, client)
    return future, next_check


def _start_pending_jobs(settings: Settings, client: LarkClient) -> bool:
    """Activate one coordinator; account count is not the HTTP concurrency limit."""

    for job in _running_jobs():
        if job.discovery_status == "pending":
            _prepare_job(job, settings, client)
            return True

    job = _claim_job()
    if job:
        _prepare_job(job, settings, client)
        return True
    return False


def _prepare_job(job: SyncJob, settings: Settings, client: LarkClient) -> None:
    current = _begin_discovery(job)
    if not current:
        return
    try:
        authorization = _authorization(current.authorization_id)
        if not authorization or authorization["status"] != "active":
            _finish_job(current, "cancelled", "Authorization is no longer active.")
            return
        access_token = _usable_access_token(current.authorization_id, authorization, client, settings)
        _refresh_profile_name(current, authorization, access_token, client, settings)
        discovered = _discover_chats(current, authorization, access_token, client, settings, chat_type="group")
        if authorization.get("body_consent_at") and authorization.get("p2p_discovery_status") not in {
            "unavailable",
            "attention_required",
        }:
            discovered += _discover_p2p(current, authorization, access_token, client, settings)
        _complete_discovery(current, discovered)
    except StaleSyncJob:
        return
    except ReauthorizationRequired:
        _mark_attention(current, "Lark authorization must be renewed.")
    except LarkApiError as exc:
        _handle_job_api_error(current, exc, discovery=True)
    except (psycopg.Error, ValueError):
        _requeue(
            current,
            delay_seconds=min(60 * (2 ** min(current.attempts, 4)), 900),
            detail="Temporary service issue; retry scheduled.",
            reset_discovery=True,
        )


def _begin_discovery(job: SyncJob) -> SyncJob | None:
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """
            UPDATE sync_jobs
            SET discovery_status = 'running', discovery_completed_at = NULL,
                lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_status = 'pending'
              AND discovery_generation = %s
            RETURNING mode, attempts, discovery_generation
            """,
            (job.id, job.discovery_generation),
        ).fetchone()
    if not row:
        return None
    return SyncJob(
        job.id,
        job.authorization_id,
        row["mode"],
        int(row["attempts"]),
        "running",
        int(row["discovery_generation"]),
    )


def _complete_discovery(job: SyncJob, discovered: int) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET discovery_status = 'succeeded', discovery_completed_at = NOW(),
                chats_discovered = %s, last_progress_at = NOW(),
                lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s AND status = 'running'
              AND discovery_status = 'running' AND discovery_generation = %s
            """,
            (discovered, job.id, job.discovery_generation),
        )


def _handle_job_api_error(job: SyncJob, exc: LarkApiError, *, discovery: bool) -> None:
    if exc.likely_auth_problem:
        _mark_attention(job, "Lark authorization or permission needs attention.")
    elif exc.retryable:
        _requeue(
            job,
            delay_seconds=exc.retry_after
            if exc.retry_after is not None
            else min(60 * (2 ** min(job.attempts, 4)), 900),
            detail="Lark is temporarily unavailable; retry scheduled.",
            code=exc.lark_code,
            http_status=exc.status_code,
            reset_discovery=discovery,
        )
    else:
        _finish_job(job, "failed", "A Lark API request could not be completed.", exc.lark_code)


def _fill_lark_slots(
    settings: Settings,
    client: LarkClient,
    executor: ThreadPoolExecutor,
    futures: dict[Future[dict[str, Any]], PageClaim],
) -> None:
    """Give distinct users the first slots, then use any spare capacity."""

    jobs = [job for job in _running_jobs() if job.discovery_status == "succeeded"]
    authorization_cache: dict[uuid.UUID, tuple[dict[str, Any], str] | None] = {}
    while len(futures) < settings.lark_sync_max_inflight:
        if not _dispatch_lark_turn(settings, client, executor, futures, jobs, authorization_cache):
            return


def _active_page_claim_counts(authorization_ids: set[uuid.UUID]) -> Counter[uuid.UUID]:
    """Read durable claims so fairness survives an accidental second worker."""

    if not authorization_ids:
        return Counter()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT authorization_id, COUNT(*) AS count
            FROM authorization_chats
            WHERE authorization_id = ANY(%s)
              AND page_claim_id IS NOT NULL AND page_lease_until > NOW()
            GROUP BY authorization_id
            """,
            (list(authorization_ids),),
        ).fetchall()
    return Counter({row["authorization_id"]: int(row["count"]) for row in rows})


def _dispatch_lark_turn(
    settings: Settings,
    client: LarkClient,
    executor: ThreadPoolExecutor,
    futures: dict[Future[dict[str, Any]], PageClaim],
    jobs: list[SyncJob],
    authorization_cache: dict[uuid.UUID, tuple[dict[str, Any], str] | None],
) -> bool:
    active_authorizations = {job.authorization_id for job in jobs}
    in_flight = _active_page_claim_counts(active_authorizations)
    fair_limit = (
        settings.lark_sync_max_inflight
        if len(active_authorizations) <= 1
        else min(settings.lark_sync_per_authorization_max_inflight, settings.lark_sync_max_inflight)
    )

    def dispatch(limit: int) -> bool:
        # Stable ties keep the oldest-progress order supplied by _running_jobs.
        for job in sorted(jobs, key=lambda item: in_flight[item.authorization_id]):
            if in_flight[job.authorization_id] >= limit:
                continue
            cached = authorization_cache.get(job.authorization_id)
            if cached is None and job.authorization_id not in authorization_cache:
                authorization = _authorization(job.authorization_id)
                if not authorization or authorization["status"] != "active":
                    _finish_job(job, "cancelled", "Authorization is no longer active.")
                    authorization_cache[job.authorization_id] = None
                    continue
                try:
                    access_token = _usable_access_token(job.authorization_id, authorization, client, settings)
                    cached = (authorization, access_token)
                except ReauthorizationRequired:
                    _mark_attention(job, "Lark authorization must be renewed.")
                    authorization_cache[job.authorization_id] = None
                    continue
                except LarkApiError as exc:
                    _handle_job_api_error(job, exc, discovery=False)
                    authorization_cache[job.authorization_id] = None
                    continue
                except (psycopg.Error, ValueError):
                    _requeue(
                        job,
                        delay_seconds=min(60 * (2 ** min(job.attempts, 4)), 900),
                        detail="Temporary service issue; retry scheduled.",
                    )
                    authorization_cache[job.authorization_id] = None
                    continue
                authorization_cache[job.authorization_id] = cached
            if not cached:
                continue
            authorization, access_token = cached
            try:
                claim = _claim_next_page(job, authorization, settings, limit)
            except (psycopg.Error, ValueError):
                _requeue(
                    job,
                    delay_seconds=min(60 * (2 ** min(job.attempts, 4)), 900),
                    detail="Temporary service issue; retry scheduled.",
                )
                continue
            if not claim:
                continue
            futures[executor.submit(_fetch_claim, client, claim, access_token)] = claim
            return True
        return False

    # First give each authorized person up to the fair share.  If other active
    # coordinators have no claimable conversation, do not leave a global slot
    # idle: the remaining user can use it for another distinct chat.
    if dispatch(fair_limit):
        return True
    if fair_limit < settings.lark_sync_max_inflight:
        return dispatch(settings.lark_sync_max_inflight)
    return False


def _fetch_claim(client: LarkClient, claim: PageClaim, access_token: str) -> dict[str, Any]:
    return client.list_chat_messages(
        access_token,
        claim.chat_id,
        page_token=claim.expected_page_token,
        start_at=claim.start_at,
        end_at=claim.end_at,
        ascending=True,
    )


def _reap_page_futures(futures: dict[Future[dict[str, Any]], PageClaim], settings: Settings) -> None:
    for future, claim in tuple(futures.items()):
        if not future.done():
            continue
        del futures[future]
        try:
            data = future.result()
        except LarkApiError as exc:
            _handle_claim_api_error(claim, exc)
            continue
        except Exception:
            LOGGER.exception("Lark page task failed without exposing message content")
            _release_retryable_page_claim(
                claim,
                delay_seconds=min(60 * (2 ** min(claim.job.attempts, 4)), 900),
                detail="Temporary service issue; this conversation will retry.",
            )
            continue
        content_cipher = message_body_cipher(settings.token_encryption_key) if claim.body_consent_at else None
        try:
            commit = _record_claimed_page(claim, _items(data), _continuation_token(data), content_cipher,
                                         fingerprint_key=settings.token_encryption_key)
        except StaleSyncJob:
            continue
        except LarkApiError as exc:
            _handle_claim_api_error(claim, exc)
            continue
        except (psycopg.Error, ValueError):
            LOGGER.exception("Lark page persistence failed without exposing message content")
            _requeue(
                claim.job,
                delay_seconds=min(60 * (2 ** min(claim.job.attempts, 4)), 900),
                detail="Temporary service issue; retry scheduled.",
                claim=claim,
            )
            continue
        if commit.committed:
            _add_job_progress(claim.job, pages=1, messages=commit.messages, own_messages=commit.own_messages)


def _schedule_member_snapshot(
    settings: Settings,
    client: LarkClient,
    executor: ThreadPoolExecutor,
    page_futures: dict[Future[dict[str, Any]], PageClaim],
    member_futures: dict[Future[MemberSnapshotMeasurement], MemberSnapshotClaim],
) -> None:
    """One roster slot shares the API admission limit without waiting for message work to end."""

    if member_futures:
        return
    if "im:chat.members:read" not in settings.oauth_scopes:
        _mark_one_member_snapshot_unavailable()
        return
    claim = _claim_member_snapshot(settings)
    if not claim:
        return
    authorization = _authorization(claim.authorization_id)
    if not authorization or authorization["status"] != "active":
        _release_member_snapshot_claim(claim, "pending")
        return
    try:
        access_token = _usable_access_token(claim.authorization_id, authorization, client, settings)
    except ReauthorizationRequired:
        _release_member_snapshot_claim(claim, "pending")
        return
    except LarkApiError:
        _release_member_snapshot_claim(claim, "pending")
        return
    member_futures[executor.submit(_read_member_snapshot, client, access_token, claim.chat_id)] = claim


def _claim_member_snapshot(settings: Settings) -> MemberSnapshotClaim | None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        # Reuse the singleton admission row as a short global mutex so multiple
        # worker processes cannot each start a roster read at the same time.
        conn.execute("SELECT id FROM lark_sync_admission_state WHERE id FOR UPDATE")
        chat = conn.execute(
            """
            SELECT access.authorization_id, access.chat_id, access.sync_generation
            FROM authorization_chats access
            JOIN chats c ON c.chat_id = access.chat_id
            JOIN authorizations a ON a.id = access.authorization_id
            WHERE a.status = 'active' AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
              AND COALESCE(c.chat_mode, 'group') <> 'p2p'
              AND (access.member_snapshot_checked_at IS NULL
                   OR access.member_snapshot_checked_at <= NOW() - (%s * INTERVAL '1 second'))
              AND (access.member_snapshot_retry_at IS NULL OR access.member_snapshot_retry_at <= NOW())
              AND (access.member_snapshot_claim_id IS NULL
                   OR access.member_snapshot_lease_until < NOW())
              AND NOT EXISTS (
                  SELECT 1 FROM authorization_chats active_snapshot
                  WHERE active_snapshot.member_snapshot_claim_id IS NOT NULL
                    AND active_snapshot.member_snapshot_lease_until > NOW()
              )
            ORDER BY EXISTS (
                SELECT 1 FROM creator_import_rows r JOIN creator_import_batches b ON b.id=r.batch_id
                WHERE r.cm_authorization_id=access.authorization_id AND r.review_status='pending'
                  AND r.status IN ('pending','preparing') AND b.status='running'
                  AND r.stats->>'analysis_stage'='person'
                  AND r.refs @> jsonb_build_array(jsonb_build_object('chat_id',access.chat_id))
              ) DESC, access.member_snapshot_checked_at NULLS FIRST, access.chat_id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (settings.member_count_refresh_seconds,),
        ).fetchone()
        if not chat:
            return None
        claim_id = uuid.uuid4()
        conn.execute(
            """
            UPDATE authorization_chats
            SET member_snapshot_claim_id = %s, member_snapshot_claimed_at = NOW(),
                member_snapshot_lease_until = NOW() + INTERVAL '10 minutes'
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
            """,
            (claim_id, chat["authorization_id"], chat["chat_id"], chat["sync_generation"]),
        )
    return MemberSnapshotClaim(
        chat["authorization_id"], chat["chat_id"], int(chat["sync_generation"]), claim_id
    )


def _reap_member_futures(
    futures: dict[Future[MemberSnapshotMeasurement], MemberSnapshotClaim],
) -> None:
    for future, claim in tuple(futures.items()):
        if not future.done():
            continue
        del futures[future]
        try:
            measurement = future.result()
        except Exception:
            LOGGER.exception("member snapshot task failed without exposing roster data")
            _release_member_snapshot_claim(claim, "pending")
            continue
        try:
            _store_claimed_member_snapshot(claim, measurement)
        except psycopg.Error as exc:
            LOGGER.warning("member snapshot commit deferred: %s; no roster data logged", type(exc).__name__)
            _release_member_snapshot_claim(claim, "pending")


def _store_claimed_member_snapshot(claim: MemberSnapshotClaim, measurement: MemberSnapshotMeasurement) -> None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        row = conn.execute(
            """
            SELECT 1
            FROM authorization_chats
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
              AND member_snapshot_claim_id = %s
            FOR UPDATE
            """,
            (claim.authorization_id, claim.chat_id, claim.generation, claim.claim_id),
        ).fetchone()
        if not row:
            return
        if measurement.retry_after_seconds is not None:
            conn.execute(
                """
                UPDATE authorization_chats
                SET member_snapshot_status = 'pending',
                    member_snapshot_retry_at = NOW() + (%s * INTERVAL '1 second'),
                    member_snapshot_claim_id = NULL, member_snapshot_claimed_at = NULL,
                    member_snapshot_lease_until = NULL
                WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
                  AND member_snapshot_claim_id = %s
                """,
                (
                    measurement.retry_after_seconds,
                    claim.authorization_id,
                    claim.chat_id,
                    claim.generation,
                    claim.claim_id,
                ),
            )
            return
        seen_at = measurement.checked_at or datetime.now(UTC)
        _store_member_snapshot(
            conn,
            claim.authorization_id,
            claim.chat_id,
            measurement,
            seen_at,
            _internal_tenant_keys(conn),
        )
        conn.execute(
            """
            UPDATE authorization_chats
            SET member_count = %s, member_count_status = %s, member_count_checked_at = %s,
                member_snapshot_status = %s, member_snapshot_checked_at = %s,
                member_snapshot_claim_id = NULL, member_snapshot_claimed_at = NULL,
                member_snapshot_lease_until = NULL, member_snapshot_retry_at = NULL
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
              AND member_snapshot_claim_id = %s
            """,
            (
                measurement.count,
                measurement.count_status,
                measurement.checked_at,
                measurement.snapshot_status,
                measurement.checked_at,
                claim.authorization_id,
                claim.chat_id,
                claim.generation,
                claim.claim_id,
            ),
        )


def _release_member_snapshot_claim(claim: MemberSnapshotClaim, status: str) -> None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        conn.execute(
            """
            UPDATE authorization_chats
            SET member_snapshot_status = %s,
                member_snapshot_checked_at = CASE WHEN %s = 'pending' THEN member_snapshot_checked_at ELSE NOW() END,
                member_snapshot_retry_at = CASE
                    WHEN %s = 'pending' THEN NOW() + INTERVAL '5 minutes'
                    ELSE NULL
                END,
                member_snapshot_claim_id = NULL, member_snapshot_claimed_at = NULL,
                member_snapshot_lease_until = NULL
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
              AND member_snapshot_claim_id = %s
            """,
            (status, status, status, claim.authorization_id, claim.chat_id, claim.generation, claim.claim_id),
        )


def _mark_one_member_snapshot_unavailable() -> None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        conn.execute(
            """
            UPDATE authorization_chats
            SET member_count_status = 'unavailable', member_snapshot_status = 'unavailable',
                member_count_checked_at = NOW(), member_snapshot_checked_at = NOW()
            WHERE (authorization_id, chat_id) IN (
                SELECT access.authorization_id, access.chat_id
                FROM authorization_chats access
                JOIN chats c ON c.chat_id = access.chat_id
                WHERE COALESCE(c.chat_mode, 'group') <> 'p2p'
                  AND access.member_snapshot_status = 'pending'
                ORDER BY access.chat_id
                LIMIT 1
            )
            """
        )


def _handle_claim_api_error(claim: PageClaim, exc: LarkApiError) -> None:
    if exc.lark_code == 231203:
        _mark_chat_not_readable(claim, code=exc.lark_code, restricted="restricted mode" in str(exc).lower())
    elif exc.likely_auth_problem:
        _mark_attention(claim.job, "Lark authorization or permission needs attention.", claim=claim)
    elif exc.retryable:
        _release_retryable_page_claim(
            claim,
            delay_seconds=exc.retry_after
            if exc.retry_after is not None
            else min(60 * (2 ** min(claim.job.attempts, 4)), 900),
            detail="Lark is temporarily unavailable; this conversation will retry.",
            code=exc.lark_code,
            http_status=exc.status_code,
        )
    else:
        _mark_chat_not_readable(claim, code=exc.lark_code)


def _finish_completed_jobs(futures: dict[Future[dict[str, Any]], PageClaim]) -> None:
    in_flight = {claim.job.id for claim in futures.values()}
    for job in _running_jobs():
        if job.discovery_status != "succeeded" or job.id in in_flight:
            continue
        _finish_and_schedule_incremental(job)


def _authorization(authorization_id: uuid.UUID) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            """
            SELECT id, open_id, status, access_token_ciphertext, refresh_token_ciphertext,
                   access_token_expires_at, refresh_token_expires_at,
                   body_consent_at, p2p_discovery_status
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
            SELECT status, access_token_ciphertext, refresh_token_ciphertext,
                   access_token_expires_at, refresh_token_expires_at
            FROM authorizations WHERE id = %s FOR UPDATE
            """,
            (authorization_id,),
        ).fetchone()
        if not row or row["status"] != "active" or not row["refresh_token_ciphertext"]:
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
            scopes = COALESCE(%s::text[], scopes),
            status = 'active', last_error = NULL, updated_at = NOW()
        WHERE id = %s
        """,
        (
            cipher.encrypt(bundle.access_token),
            cipher.encrypt(bundle.refresh_token or ""),
            _expiry(bundle.expires_in),
            _expiry(bundle.refresh_expires_in),
            list(dict.fromkeys(bundle.scope.split())) if bundle.scope is not None else None,
            authorization_id,
        ),
    )


def _refresh_profile_name(
    job: SyncJob, authorization: dict[str, Any], access_token: str, client: LarkClient, settings: Settings
) -> None:
    """One optional name refresh per discovery job; failure never revokes sync."""
    try:
        snapshot = _authorization(job.authorization_id)
        if not snapshot or snapshot["status"] != "active" or snapshot["open_id"] != authorization["open_id"]:
            return
        if TokenCipher(settings.token_encryption_key).decrypt(snapshot["access_token_ciphertext"]) != access_token:
            return
        profile = client.user_info(access_token)
        if profile.open_id != snapshot["open_id"]:
            return
        with connection() as conn, conn.transaction():
            lock_sync_material(conn)
            current = conn.execute(
                """SELECT open_id,status,access_token_ciphertext FROM authorizations WHERE id=%s FOR UPDATE""",
                (job.authorization_id,),
            ).fetchone()
            if (
                not current
                or current["status"] != "active"
                or current["open_id"] != profile.open_id
                or current["access_token_ciphertext"] != snapshot["access_token_ciphertext"]
            ):
                return
            _assert_discovery_current(conn, job)
            identity_profiles.store_profile_avatar(
                conn, profile.open_id, profile.avatar_url, avatar_present=profile.avatar_present,
            )
            label = _upsert_lark_identity(
                conn,
                profile.open_id,
                display_name=profile.name,
                en_name=profile.en_name,
                en_name_present=profile.en_name_present,
                authoritative=True,
            )
            if label:
                conn.execute(
                    """UPDATE authorizations SET display_name=%s
                             WHERE id=%s AND display_name IS DISTINCT FROM %s""",
                    (label, job.authorization_id, label),
                )
    except Exception:
        # This optional profile refresh must not change the authorization state
        # or log provider payloads. The existing IM sync remains authoritative.
        LOGGER.info("optional profile-name refresh unavailable; message sync continues")


def _discover_chats(
    job: SyncJob,
    authorization: dict[str, Any],
    access_token: str,
    client: LarkClient,
    settings: Settings,
    *,
    chat_type: str,
) -> int:
    """Discover one conversation type and retain its user-specific visibility."""

    page_token: str | None = None
    discovered = 0
    seen_at = datetime.now(UTC)
    while True:
        _renew_lease(job)
        data = client.list_chats(access_token, page_token, types="p2p" if chat_type == "p2p" else None)
        rows: list[
            tuple[
                str,
                str | None,
                bool,
                str,
                str,
                MemberSnapshotMeasurement | None,
                str | None,
                str | None,
            ]
        ] = []
        for chat in _items(data):
            chat_id = _text(chat.get("chat_id"))
            if not chat_id:
                continue
            chat_mode = _text(chat.get("chat_mode")) or chat_type
            if chat_type == "group" and chat_mode == "p2p":
                continue
            measurement: MemberSnapshotMeasurement | None = None
            rows.append(
                (
                    chat_id,
                    _text(chat.get("name")),
                    _bool(chat.get("external")),
                    _text(chat.get("chat_status")) or "normal",
                    chat_mode,
                    measurement,
                    _text(chat.get("p2p_target_id")) if chat_type == "p2p" else None,
                    _text(chat.get("p2p_target_type")) if chat_type == "p2p" else None,
                )
            )

        with connection() as conn, conn.transaction():
            lock_sync_material(conn)
            _assert_discovery_current(conn, job)
            internal_tenant_keys = _internal_tenant_keys(conn)
            for (
                chat_id,
                name,
                external,
                chat_status,
                chat_mode,
                measurement,
                counterpart_id,
                counterpart_type,
            ) in rows:
                member_count = measurement.count if measurement else None
                member_count_status = measurement.count_status if measurement else "pending"
                member_count_checked_at = measurement.checked_at if measurement else None
                snapshot_status = measurement.snapshot_status if measurement else "pending"
                snapshot_checked_at = measurement.checked_at if measurement else None
                conn.execute(
                    """
                    INSERT INTO chats (
                        chat_id, name, external, chat_status, chat_mode,
                        first_seen_at, last_seen_at, metadata_updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chat_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        external = EXCLUDED.external,
                        chat_status = EXCLUDED.chat_status,
                        chat_mode = EXCLUDED.chat_mode,
                        last_seen_at = EXCLUDED.last_seen_at,
                        metadata_updated_at = EXCLUDED.metadata_updated_at
                    """,
                    (
                        chat_id,
                        name,
                        external,
                        chat_status,
                        chat_mode,
                        seen_at,
                        seen_at,
                        seen_at,
                    ),
                )
                counterpart_open_id = _p2p_counterpart_open_id(counterpart_id, counterpart_type)
                conn.execute(
                    """
                    INSERT INTO authorization_chats (
                        authorization_id, chat_id, is_visible, last_seen_at,
                        member_count, member_count_status, member_count_checked_at,
                        member_snapshot_status, member_snapshot_checked_at,
                        p2p_counterpart_id, p2p_counterpart_type,
                        p2p_counterpart_open_id, p2p_counterpart_seen_at
                    ) VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL)
                    ON CONFLICT (authorization_id, chat_id) DO UPDATE SET
                        is_visible = TRUE,
                        last_seen_at = EXCLUDED.last_seen_at,
                        member_count = CASE
                            WHEN EXCLUDED.member_count_checked_at IS NULL THEN authorization_chats.member_count
                            ELSE EXCLUDED.member_count
                        END,
                        member_count_status = CASE
                            WHEN EXCLUDED.member_count_checked_at IS NULL THEN authorization_chats.member_count_status
                            ELSE EXCLUDED.member_count_status
                        END,
                        member_count_checked_at = COALESCE(
                            EXCLUDED.member_count_checked_at, authorization_chats.member_count_checked_at
                        ),
                        member_snapshot_status = CASE
                            WHEN EXCLUDED.member_snapshot_checked_at IS NULL
                                THEN authorization_chats.member_snapshot_status
                            ELSE EXCLUDED.member_snapshot_status
                        END,
                        member_snapshot_checked_at = COALESCE(
                            EXCLUDED.member_snapshot_checked_at, authorization_chats.member_snapshot_checked_at
                        ),
                        p2p_counterpart_id = COALESCE(
                            EXCLUDED.p2p_counterpart_id, authorization_chats.p2p_counterpart_id
                        ),
                        p2p_counterpart_type = COALESCE(
                            EXCLUDED.p2p_counterpart_type, authorization_chats.p2p_counterpart_type
                        ),
                        -- Keep the current user FK until the identity row exists below.  An
                        -- explicit non-user target is a real removal, not missing metadata.
                        p2p_counterpart_open_id = CASE
                            WHEN EXCLUDED.p2p_counterpart_type IS NOT NULL
                              AND EXCLUDED.p2p_counterpart_type <> 'user' THEN NULL
                            ELSE authorization_chats.p2p_counterpart_open_id
                        END,
                        p2p_counterpart_seen_at = CASE
                            WHEN EXCLUDED.p2p_counterpart_type IS NOT NULL
                              AND EXCLUDED.p2p_counterpart_type <> 'user' THEN NULL
                            ELSE authorization_chats.p2p_counterpart_seen_at
                        END
                    """,
                    (
                        authorization["id"],
                        chat_id,
                        seen_at,
                        member_count,
                        member_count_status,
                        member_count_checked_at,
                        snapshot_status,
                        snapshot_checked_at,
                        counterpart_id,
                        counterpart_type,
                    ),
                )
                # Page commits lock access before identities/contacts. Keep
                # that order, then fill the FK after a new identity exists.
                if counterpart_open_id:
                    _upsert_lark_identity(conn, counterpart_open_id, seen_at=seen_at)
                    _ensure_contact_for_identity(
                        conn,
                        counterpart_open_id,
                        tenant_classification="unresolved",
                        identity_type="user",
                        seen_at=seen_at,
                    )
                    conn.execute(
                        """UPDATE authorization_chats
                           SET p2p_counterpart_open_id=%s,p2p_counterpart_seen_at=%s
                           WHERE authorization_id=%s AND chat_id=%s""",
                        (counterpart_open_id, seen_at, authorization["id"], chat_id),
                    )
                if measurement:
                    _store_member_snapshot(
                        conn,
                        authorization["id"],
                        chat_id,
                        measurement,
                        seen_at,
                        internal_tenant_keys,
                    )
                discovered += 1
            _ensure_internal_contact_roles(conn)
        if not _has_more(data):
            break
        page_token = _text(data.get("page_token"))
        if not page_token:
            raise LarkApiError(502, None, "Lark chat pagination did not return a continuation token")

    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        _assert_discovery_current(conn, job)
        conn.execute(
            """
            UPDATE authorization_chats AS access
            SET is_visible = FALSE,
                manual_backfill_requested_at = NULL,
                manual_backfill_started_at = NULL,
                sync_generation = sync_generation + 1
            FROM chats AS c
            WHERE access.authorization_id = %s
              AND access.chat_id = c.chat_id
              AND COALESCE(c.chat_mode, 'group') = %s
              AND access.last_seen_at < %s
            """,
            (authorization["id"], chat_type, seen_at),
        )
    return discovered


def _assert_discovery_current(conn: psycopg.Connection, job: SyncJob) -> None:
    row = conn.execute(
        """
        SELECT 1
        FROM sync_jobs
        WHERE id = %s AND status = 'running' AND discovery_status = 'running'
          AND discovery_generation = %s
        FOR UPDATE
        """,
        (job.id, job.discovery_generation),
    ).fetchone()
    if not row:
        raise StaleSyncJob


def _discover_p2p(
    job: SyncJob,
    authorization: dict[str, Any],
    access_token: str,
    client: LarkClient,
    settings: Settings,
) -> int:
    """Treat Lark's evolving P2P-list support as an explicit per-user capability."""

    try:
        discovered = _discover_chats(job, authorization, access_token, client, settings, chat_type="p2p")
    except LarkApiError as exc:
        if exc.retryable:
            raise
        status = "attention_required" if exc.likely_auth_problem else "unavailable"
        detail = (
            "Lark authorization or P2P permission must be renewed."
            if status == "attention_required"
            else "Lark does not currently return this user's P2P conversation list."
        )
        _set_p2p_discovery(job, status, detail)
        return 0
    _set_p2p_discovery(job, "succeeded", None)
    return discovered


def _set_p2p_discovery(job: SyncJob, status: str, detail: str | None) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE authorizations
            SET p2p_discovery_status = %s, p2p_last_checked_at = NOW(),
                p2p_last_error = %s, updated_at = NOW()
            WHERE id = %s
              AND EXISTS (
                  SELECT 1 FROM sync_jobs
                  WHERE id = %s AND status = 'running' AND discovery_status = 'running'
                    AND discovery_generation = %s
              )
            """,
            (status, detail, job.authorization_id, job.id, job.discovery_generation),
        )


def _p2p_counterpart_open_id(counterpart_id: str | None, counterpart_type: str | None) -> str | None:
    """Only a Lark user target has an open ID suitable for a contact."""

    return counterpart_id if counterpart_type == "user" else None


def _read_member_snapshot(client: LarkClient, access_token: str, chat_id: str) -> MemberSnapshotMeasurement:
    checked_at = datetime.now(UTC)
    page_token: str | None = None
    members: list[dict[str, Any]] = []
    reported_total: int | None = None
    partial = False
    try:
        while True:
            data = client.list_chat_members(access_token, chat_id, page_token)
            if reported_total is None:
                reported_total = _nonnegative_int(data.get("user_total"))
            members.extend(_member_records(data))
            partial = partial or bool(data.get("truncations"))
            if not _has_more(data):
                break
            page_token = _continuation_token(data)
    except LarkApiError as exc:
        status = "pending" if exc.retryable else "unavailable"
        LOGGER.info("member snapshot unavailable: status=%s code=%s", exc.status_code, exc.lark_code)
        if exc.retryable:
            return MemberSnapshotMeasurement(
                count=None,
                count_status="pending",
                snapshot_status="pending",
                checked_at=None,
                retry_after_seconds=exc.retry_after if exc.retry_after is not None else 300,
            )
        return MemberSnapshotMeasurement(None, status, status, checked_at)

    unique_members = {member["open_id"]: member for member in members}
    total = reported_total if reported_total is not None else len(unique_members) if unique_members else None
    if total is None:
        LOGGER.info("member snapshot response omitted a usable user total")
        return MemberSnapshotMeasurement(None, "unavailable", "unavailable", checked_at)
    snapshot_status = "partial" if partial or len(unique_members) != total else "succeeded"
    return MemberSnapshotMeasurement(
        count=total,
        count_status="succeeded",
        snapshot_status=snapshot_status,
        checked_at=checked_at,
        members=tuple(unique_members.values()),
    )


def _member_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("users")
    if not isinstance(raw, list):
        raw = data.get("items")
    records: list[dict[str, Any]] = []
    for member in raw if isinstance(raw, list) else []:
        if not isinstance(member, dict):
            continue
        open_id = _text(member.get("member_id")) or _text(member.get("open_id"))
        if not open_id:
            continue
        records.append(
            {
                "open_id": open_id,
                "tenant_key": _text(member.get("tenant_key")),
                "name": _text(member.get("name")),
                "en_name": _text(member.get("en_name")),
                "en_name_present": "en_name" in member,
                "display_name": format_display_name(_text(member.get("name")), _text(member.get("en_name"))),
                "member_type": _text(member.get("member_type")) or "user",
            }
        )
    return records


def _store_member_snapshot(
    conn: psycopg.Connection,
    authorization_id: uuid.UUID,
    chat_id: str,
    measurement: MemberSnapshotMeasurement,
    seen_at: datetime,
    internal_tenant_keys: set[str],
) -> None:
    # OAuth-owned profiles have a separate verified refresh path. A group API's
    # display variant must not replace their original/English profile fields.
    authorized = {row["open_id"] for row in conn.execute(
        "SELECT open_id FROM authorizations WHERE open_id=ANY(%s::text[])",
        ([member["open_id"] for member in measurement.members],),
    ).fetchall()}
    for member in measurement.members:
        open_id = member["open_id"]
        assert open_id is not None
        label = _upsert_lark_identity(
            conn,
            open_id,
            tenant_key=member["tenant_key"],
            display_name=member.get("name"),
            en_name=member.get("en_name"),
            en_name_present=bool(member.get("en_name_present")),
            authoritative=open_id not in authorized,
            identity_type=member["member_type"] or "user",
            seen_at=seen_at,
        )
        _ensure_contact_for_identity(
            conn,
            open_id,
            tenant_classification=_tenant_classification(
                tenant_key=member["tenant_key"],
                identity_type=member["member_type"] or "user",
                internal_tenant_keys=internal_tenant_keys,
            ),
            identity_type=member["member_type"] or "user",
            display_name=label,
            seen_at=seen_at,
        )
        conn.execute(
            """
            INSERT INTO chat_members (
                chat_id, open_id, tenant_key, display_name, member_type,
                first_seen_at, last_seen_at, last_observed_by_authorization_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, open_id) DO UPDATE SET
                tenant_key = COALESCE(EXCLUDED.tenant_key, chat_members.tenant_key),
                display_name = COALESCE(EXCLUDED.display_name, chat_members.display_name),
                member_type = COALESCE(EXCLUDED.member_type, chat_members.member_type),
                last_seen_at = EXCLUDED.last_seen_at,
                last_observed_by_authorization_id = EXCLUDED.last_observed_by_authorization_id
            """,
            (
                chat_id,
                open_id,
                member["tenant_key"],
                label,
                member["member_type"] or "user",
                seen_at,
                seen_at,
                authorization_id,
            ),
        )

    creator_group_matching.touch_chats(conn, [chat_id])


def _upsert_lark_identity(
    conn: psycopg.Connection,
    open_id: str,
    *,
    tenant_key: str | None = None,
    display_name: str | None = None,
    identity_type: str = "user",
    seen_at: datetime | None = None,
    en_name: str | None = None,
    en_name_present: bool = False,
    authoritative: bool = False,
) -> str | None:
    observed_at = seen_at or datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO lark_identities (
            open_id, tenant_key, display_name, identity_type, first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (open_id) DO UPDATE SET
            tenant_key = COALESCE(EXCLUDED.tenant_key, lark_identities.tenant_key),
            identity_type = COALESCE(EXCLUDED.identity_type, lark_identities.identity_type),
            last_seen_at = EXCLUDED.last_seen_at
        """,
        (open_id, tenant_key, display_name, identity_type, observed_at, observed_at),
    )
    return update_identity_names(
        conn, open_id, name=display_name, en_name=en_name, en_name_present=en_name_present, authoritative=authoritative
    )


def _internal_tenant_keys(conn: psycopg.Connection) -> set[str]:
    """Tenant keys proven internal by an authorised user's own identity or message."""

    rows = conn.execute(
        """
        SELECT DISTINCT tenant_key
        FROM (
            SELECT identity.tenant_key
            FROM authorizations auth
            JOIN lark_identities identity ON identity.open_id = auth.open_id
            WHERE identity.tenant_key IS NOT NULL
            UNION
            SELECT message.sender_tenant_key
            FROM authorizations auth
            JOIN chat_messages message ON message.sender_open_id = auth.open_id
            WHERE message.sender_tenant_key IS NOT NULL
        ) tenant_facts
        """
    ).fetchall()
    return {row["tenant_key"] for row in rows}


def _tenant_classification(
    *, tenant_key: str | None, identity_type: str, internal_tenant_keys: set[str]
) -> str:
    if identity_type != "user":
        return "internal"
    if not tenant_key:
        return "unresolved"
    return "internal" if tenant_key in internal_tenant_keys else "known_external"


def _refresh_tenant_classifications(conn: psycopg.Connection, internal_tenant_keys: set[str]) -> None:
    """Reclassify identities when a newly authorised tenant key is learned."""

    # A page commit already holds sender/message locks. Do not also lock every
    # unchanged identity and block unrelated AI evidence transactions.
    conn.execute(
        """
        UPDATE lark_identities
        SET tenant_classification = CASE
            WHEN identity_type <> 'user' THEN 'internal'
            WHEN tenant_key IS NULL THEN 'unresolved'
            WHEN tenant_key = ANY(%s) THEN 'internal'
            ELSE 'known_external'
        END
        WHERE tenant_classification IS DISTINCT FROM CASE
            WHEN identity_type <> 'user' THEN 'internal'
            WHEN tenant_key IS NULL THEN 'unresolved'
            WHEN tenant_key = ANY(%s) THEN 'internal'
            ELSE 'known_external'
        END
        """,
        (list(internal_tenant_keys), list(internal_tenant_keys)),
    )
    _ensure_internal_contact_roles(conn)


def _ensure_internal_contact_roles(conn: psycopg.Connection) -> None:
    """Preserve manual decisions; trusted tenant membership outranks only AI roles."""
    targets = conn.execute(
        """
        SELECT contact.id FROM contacts contact
        WHERE EXISTS (
            SELECT 1 FROM lark_identities identity WHERE identity.contact_id=contact.id
              AND identity.identity_type='user' AND identity.tenant_classification='internal'
        )
          AND NOT EXISTS (
            SELECT 1 FROM contact_roles role WHERE role.contact_id=contact.id AND role.role='internal_colleague'
              AND role.source NOT IN ('ai_auto_confirmed','lark_internal_tenant')
          )
          AND NOT EXISTS (
            SELECT 1 FROM contact_roles role WHERE role.contact_id=contact.id AND role.role='internal_colleague'
              AND role.status='confirmed' AND role.source='lark_internal_tenant'
              AND role.is_primary=NOT EXISTS (
                SELECT 1 FROM contact_roles protected WHERE protected.contact_id=contact.id
                  AND protected.status='confirmed' AND protected.is_primary AND protected.role<>'internal_colleague'
                  AND protected.source NOT IN ('ai_auto_confirmed','lark_internal_tenant')
              )
          )
        ORDER BY contact.id FOR UPDATE OF contact SKIP LOCKED
        """
    ).fetchall()
    if not targets:
        return
    contact_ids = [row["id"] for row in targets]
    # Manual review locks the same contact. Skip a concurrent review instead of
    # blocking message synchronization; the next metadata refresh will retry it.
    conn.execute(
        """
        UPDATE contact_roles role
        SET is_primary = FALSE, updated_at = NOW()
        WHERE role.is_primary AND role.status = 'confirmed'
          AND role.contact_id=ANY(%s::uuid[]) AND role.source='ai_auto_confirmed'
          AND role.role <> 'internal_colleague'
        """, (contact_ids,),
    )
    conn.execute(
        """
        INSERT INTO contact_roles (contact_id, role, status, source, is_primary, reviewed_at)
        SELECT contact_id,'internal_colleague','confirmed','lark_internal_tenant',
               NOT EXISTS (SELECT 1 FROM contact_roles role WHERE role.contact_id=target.contact_id
                 AND role.status='confirmed' AND role.is_primary AND role.role<>'internal_colleague'),NOW()
        FROM unnest(%s::uuid[]) target(contact_id)
        ON CONFLICT (contact_id, role) DO UPDATE SET
            status = 'confirmed', source = 'lark_internal_tenant', is_primary = EXCLUDED.is_primary,
            reviewed_at = EXCLUDED.reviewed_at, updated_at = NOW()
        WHERE contact_roles.source IN ('ai_auto_confirmed','lark_internal_tenant')
          AND (contact_roles.status,contact_roles.source,contact_roles.is_primary)
            IS DISTINCT FROM ('confirmed','lark_internal_tenant',EXCLUDED.is_primary)
        """, (contact_ids,),
    )


def _ensure_contact_for_identity(
    conn: psycopg.Connection,
    open_id: str,
    *,
    tenant_classification: str,
    identity_type: str,
    display_name: str | None = None,
    seen_at: datetime | None = None,
) -> None:
    """Link one human Lark identity to one neutral contact without inferring a role."""

    conn.execute(
        """
        UPDATE lark_identities
        SET tenant_classification = CASE
            WHEN %s = 'unresolved' AND tenant_classification <> 'unresolved'
                THEN tenant_classification
            ELSE %s
        END
        WHERE open_id = %s
        """,
        (tenant_classification, tenant_classification, open_id),
    )
    if identity_type != "user":
        return

    identity = conn.execute(
        "SELECT contact_id, display_name FROM lark_identities WHERE open_id = %s FOR UPDATE",
        (open_id,),
    ).fetchone()
    if not identity:
        return
    contact_id = identity["contact_id"]
    if not contact_id:
        contact_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO contacts (id, display_name, entity_type, created_at, updated_at)
            VALUES (%s, %s, 'person', %s, %s)
            """,
            (
                contact_id,
                identity["display_name"] or display_name,
                seen_at or datetime.now(UTC),
                seen_at or datetime.now(UTC),
            ),
        )
        conn.execute("UPDATE lark_identities SET contact_id = %s WHERE open_id = %s", (contact_id, open_id))
        return
    if identity["display_name"] or display_name:
        conn.execute(
            """
            UPDATE contacts
            SET display_name = COALESCE(display_name, %s), updated_at = NOW()
            WHERE id = %s
            """,
            (identity["display_name"] or display_name, contact_id),
        )


def _claim_next_page(
    job: SyncJob,
    authorization: dict[str, Any],
    settings: Settings,
    claim_limit: int | None = None,
) -> PageClaim | None:
    """Lock one conversation cursor; another worker can only claim another chat."""

    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        current = conn.execute(
            """
            SELECT mode
            FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_status = 'succeeded'
              AND discovery_generation = %s
            FOR UPDATE
            """,
            (job.id, job.discovery_generation),
        ).fetchone()
        if not current:
            return None
        claim_limit = claim_limit or settings.lark_sync_per_authorization_max_inflight
        active_claims = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM authorization_chats
            WHERE authorization_id = %s AND page_claim_id IS NOT NULL
              AND page_lease_until > NOW()
            """,
            (job.authorization_id,),
        ).fetchone()
        if int(active_claims["count"]) >= claim_limit:
            return None
        mode = "backfill" if current["mode"] == "backfill" or _has_pending_backfill_in_connection(
            conn, job.authorization_id
        ) else "incremental"
        if mode != current["mode"]:
            conn.execute("UPDATE sync_jobs SET mode = %s, updated_at = NOW() WHERE id = %s", (mode, job.id))

        if mode == "backfill":
            _reset_one_requested_backfill(conn, job.authorization_id)
            chat = _select_backfill_chat_for_claim(conn, job.authorization_id)
        else:
            chat = _select_incremental_chat_for_claim(conn, job.authorization_id, job.id)
        if not chat:
            return None

        now = datetime.now(UTC)
        generation = int(chat["sync_generation"])
        if mode == "backfill":
            start_at = chat["backfill_window_start_at"]
            end_at = chat["backfill_window_end_at"]
            if not end_at:
                end_at = now
                start_at = (
                    None
                    if chat["manual_backfill_requested_at"]
                    else now - timedelta(days=settings.sync_lookback_days)
                    if settings.sync_lookback_days
                    else None
                )
            expected_page_token = chat["backfill_page_token"]
        else:
            same_job = chat["incremental_job_id"] == job.id
            end_at = chat["incremental_window_end_at"] if same_job else None
            start_at = chat["incremental_window_start_at"] if same_job else None
            if not end_at:
                end_at = now
                last_sync = chat["last_incremental_at"] or chat["last_backfill_at"]
                start_at = (
                    last_sync - timedelta(seconds=settings.sync_overlap_seconds)
                    if last_sync
                    else end_at - timedelta(days=settings.sync_lookback_days or 1)
                )
            expected_page_token = chat["incremental_page_token"] if same_job else None

        claim_id = uuid.uuid4()
        conn.execute(
            """
            UPDATE authorization_chats
            SET backfill_started_at = CASE WHEN %s = 'backfill' THEN COALESCE(backfill_started_at, NOW())
                    ELSE backfill_started_at END,
                backfill_window_start_at = CASE WHEN %s = 'backfill' THEN %s ELSE backfill_window_start_at END,
                backfill_window_end_at = CASE WHEN %s = 'backfill' THEN %s ELSE backfill_window_end_at END,
                incremental_job_id = CASE WHEN %s = 'incremental' THEN %s ELSE incremental_job_id END,
                incremental_page_token = CASE WHEN %s = 'incremental' THEN %s ELSE incremental_page_token END,
                incremental_window_start_at = CASE WHEN %s = 'incremental' THEN %s ELSE incremental_window_start_at END,
                incremental_window_end_at = CASE WHEN %s = 'incremental' THEN %s ELSE incremental_window_end_at END,
                page_claim_id = %s, page_claim_job_id = %s, page_claim_mode = %s,
                page_claimed_at = NOW(), page_lease_until = NOW() + INTERVAL '10 minutes',
                page_retry_at = CASE WHEN sync_status = 'not_readable' THEN NOW() + INTERVAL '1 day'
                    ELSE NULL END,
                last_error = CASE WHEN sync_status = 'not_readable' THEN last_error ELSE NULL END,
                sync_status = CASE WHEN sync_status = 'not_readable' THEN sync_status ELSE 'syncing' END
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
            """,
            (
                mode,
                mode,
                start_at,
                mode,
                end_at,
                mode,
                job.id,
                mode,
                expected_page_token,
                mode,
                start_at,
                mode,
                end_at,
                claim_id,
                job.id,
                mode,
                job.authorization_id,
                chat["chat_id"],
                generation,
            ),
        )
    assert end_at is not None
    return PageClaim(
        job=job,
        chat_id=chat["chat_id"],
        mode=mode,
        generation=generation,
        claim_id=claim_id,
        expected_page_token=expected_page_token,
        start_at=start_at,
        end_at=end_at,
        author_open_id=authorization["open_id"],
        body_consent_at=authorization.get("body_consent_at"),
    )


def _reset_one_requested_backfill(conn: psycopg.Connection, authorization_id: uuid.UUID) -> None:
    row = conn.execute(
        """
        SELECT access.authorization_id, access.chat_id
        FROM authorization_chats access
        JOIN chats c ON c.chat_id = access.chat_id
        WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
          AND COALESCE(c.chat_status, 'normal') = 'normal'
          AND access.manual_backfill_requested_at IS NOT NULL
          AND access.manual_backfill_started_at IS NULL
        ORDER BY access.manual_backfill_requested_at, access.chat_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """,
        (authorization_id,),
    ).fetchone()
    if not row:
        return
    conn.execute(
        """
        UPDATE authorization_chats
        SET backfill_page_token = NULL, backfill_complete = FALSE, backfill_started_at = NULL,
            backfill_window_start_at = NULL, backfill_window_end_at = NULL,
            incremental_page_token = NULL, incremental_job_id = NULL,
            incremental_window_start_at = NULL, incremental_window_end_at = NULL,
            page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
            page_claimed_at = NULL, page_lease_until = NULL,
            page_retry_at = NULL,
            sync_status = 'pending', last_error = NULL, manual_backfill_started_at = NOW(),
            sync_generation = sync_generation + 1
        WHERE authorization_id = %s AND chat_id = %s
        """,
        (row["authorization_id"], row["chat_id"]),
    )


def _select_backfill_chat_for_claim(conn: psycopg.Connection, authorization_id: uuid.UUID) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT access.*
        FROM authorization_chats access
        JOIN chats c ON c.chat_id = access.chat_id
        WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
          AND COALESCE(c.chat_status, 'normal') = 'normal'
          AND (access.page_claim_id IS NULL OR access.page_lease_until < NOW())
          AND (access.page_retry_at IS NULL OR access.page_retry_at <= NOW())
          AND (access.manual_backfill_requested_at IS NOT NULL OR NOT access.backfill_complete)
        ORDER BY
            CASE WHEN access.manual_backfill_requested_at IS NOT NULL THEN 0 ELSE 1 END,
            access.manual_backfill_requested_at NULLS LAST, access.discovered_at, access.chat_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """,
        (authorization_id,),
    ).fetchone()


def _select_incremental_chat_for_claim(
    conn: psycopg.Connection, authorization_id: uuid.UUID, job_id: uuid.UUID
) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT access.*
        FROM authorization_chats access
        JOIN chats c ON c.chat_id = access.chat_id
        WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
          AND COALESCE(c.chat_status, 'normal') = 'normal'
          AND access.backfill_complete
          AND (access.page_claim_id IS NULL OR access.page_lease_until < NOW())
          AND (access.page_retry_at IS NULL OR access.page_retry_at <= NOW())
          AND (
            (access.incremental_job_id = %s AND access.incremental_page_token IS NOT NULL)
            OR access.incremental_job_id IS DISTINCT FROM %s
          )
        ORDER BY
            CASE WHEN access.incremental_job_id = %s AND access.incremental_page_token IS NOT NULL THEN 0 ELSE 1 END,
            COALESCE(access.last_incremental_at, access.last_backfill_at), access.chat_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """,
        (authorization_id, job_id, job_id, job_id),
    ).fetchone()


def _has_pending_backfill_in_connection(conn: psycopg.Connection, authorization_id: uuid.UUID) -> bool:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM authorization_chats access
            JOIN chats c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
              AND (access.sync_status <> 'not_readable'
                   OR access.page_retry_at IS NULL OR access.page_retry_at <= NOW())
              AND (
                    access.manual_backfill_requested_at IS NOT NULL
                    OR NOT access.backfill_complete
              )
        ) AS pending
        """,
        (authorization_id,),
    ).fetchone()
    return bool(row["pending"])


def _record_message_sender_identity(
    conn: psycopg.Connection,
    message: dict[str, Any],
    *,
    is_own: bool,
    internal_tenant_keys: set[str],
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Persist sender identity facts; display names never act as identity keys."""

    metadata = _sender_metadata(message)
    sender_id, sender_id_type, sender_open_id, sender_tenant_key, sender_type, sender_name = metadata
    if is_own and sender_tenant_key:
        internal_tenant_keys.add(sender_tenant_key)
    if sender_open_id:
        identity_type = sender_type or "user"
        _upsert_lark_identity(
            conn,
            sender_open_id,
            tenant_key=sender_tenant_key,
            display_name=sender_name,
            identity_type=identity_type,
        )
        _ensure_contact_for_identity(
            conn,
            sender_open_id,
            tenant_classification=_tenant_classification(
                tenant_key=sender_tenant_key,
                identity_type=identity_type,
                internal_tenant_keys=internal_tenant_keys,
            ),
            identity_type=identity_type,
            display_name=sender_name,
        )
    return metadata


def _record_claimed_page(
    claim: PageClaim,
    messages: list[dict[str, Any]],
    next_page_token: str | None,
    content_cipher: TokenCipher | None,
    *,
    fingerprint_key: bytes | None = None,
) -> PageCommit:
    """Persist a fetched page only while its exact cursor claim is still current."""

    inserted = 0
    own_inserted = 0
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        # Keep the same lock order as `_claim_next_page`: coordinator first,
        # then its conversation row. This avoids a claim/commit deadlock when
        # several page fetches return together.
        current_job = conn.execute(
            """
            SELECT 1
            FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_status = 'succeeded'
              AND discovery_generation = %s
            FOR UPDATE
            """,
            (claim.job.id, claim.job.discovery_generation),
        ).fetchone()
        if not current_job:
            return PageCommit(False)
        current_claim = conn.execute(
            """
            SELECT 1
            FROM authorization_chats access
            WHERE access.authorization_id = %s AND access.chat_id = %s
              AND access.sync_generation = %s
              AND access.page_claim_id = %s AND access.page_claim_job_id = %s
              AND access.page_claim_mode = %s
              AND access.page_lease_until > NOW()
              AND (
                (%s = 'backfill' AND access.backfill_page_token IS NOT DISTINCT FROM %s)
                OR (%s = 'incremental' AND access.incremental_page_token IS NOT DISTINCT FROM %s)
              )
            FOR UPDATE
            """,
            (
                claim.job.authorization_id,
                claim.chat_id,
                claim.generation,
                claim.claim_id,
                claim.job.id,
                claim.mode,
                claim.mode,
                claim.expected_page_token,
                claim.mode,
                claim.expected_page_token,
            ),
        ).fetchone()
        if not current_claim:
            return PageCommit(False)
        internal_tenant_keys = _internal_tenant_keys(conn)
        for message in messages:
            message_id = _text(message.get("message_id"))
            if not message_id:
                continue
            is_own = _is_author_message(message, claim.author_open_id)
            (
                sender_id,
                sender_id_type,
                sender_open_id,
                sender_tenant_key,
                sender_type,
                sender_name,
            ) = _record_message_sender_identity(
                conn,
                message,
                is_own=is_own,
                internal_tenant_keys=internal_tenant_keys,
            )
            is_deleted = _bool(message.get("deleted"))
            body_content = _body_content(message)
            body_ciphertext = (
                content_cipher.encrypt(body_content) if content_cipher and body_content and not is_deleted else None
            )
            relations = _message_relation_metadata(message)
            for mention in relations["mentions"] or []:
                if mention.get("id_type") != "open_id" or not mention.get("id"):
                    continue
                mention_open_id = mention["id"]
                mention_tenant_key = mention.get("tenant_key")
                mention_name = mention.get("name")
                _upsert_lark_identity(
                    conn,
                    mention_open_id,
                    tenant_key=mention_tenant_key,
                    display_name=mention_name,
                    identity_type="user",
                )
                _ensure_contact_for_identity(
                    conn,
                    mention_open_id,
                    tenant_classification=_tenant_classification(
                        tenant_key=mention_tenant_key,
                        identity_type="user",
                        internal_tenant_keys=internal_tenant_keys,
                    ),
                    identity_type="user",
                    display_name=mention_name,
                )
            conn.execute(
                """
                INSERT INTO chat_messages (
                    chat_id, message_id, message_create_time, message_update_time,
                    message_type, body_ciphertext, body_fingerprint,
                    root_id, parent_id, thread_id, mentions_json,
                    message_app_link, message_position, thread_message_position,
                    sender_id, sender_id_type, sender_open_id, sender_tenant_key, sender_type,
                    is_deleted, first_indexed_at, last_observed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, NOW(), NOW()
                )
                ON CONFLICT (chat_id, message_id) DO UPDATE SET
                    message_create_time = COALESCE(chat_messages.message_create_time, EXCLUDED.message_create_time),
                    message_update_time = COALESCE(EXCLUDED.message_update_time, chat_messages.message_update_time),
                    message_type = COALESCE(EXCLUDED.message_type, chat_messages.message_type),
                    body_ciphertext = CASE
                        WHEN EXCLUDED.is_deleted THEN NULL
                        WHEN EXCLUDED.body_ciphertext IS NULL THEN chat_messages.body_ciphertext
                        ELSE EXCLUDED.body_ciphertext
                    END,
                    body_fingerprint = CASE WHEN EXCLUDED.is_deleted THEN NULL
                        ELSE COALESCE(EXCLUDED.body_fingerprint, chat_messages.body_fingerprint) END,
                    root_id = COALESCE(EXCLUDED.root_id, chat_messages.root_id),
                    parent_id = COALESCE(EXCLUDED.parent_id, chat_messages.parent_id),
                    thread_id = COALESCE(EXCLUDED.thread_id, chat_messages.thread_id),
                    mentions_json = COALESCE(EXCLUDED.mentions_json, chat_messages.mentions_json),
                    message_app_link = COALESCE(EXCLUDED.message_app_link, chat_messages.message_app_link),
                    message_position = COALESCE(EXCLUDED.message_position, chat_messages.message_position),
                    thread_message_position = COALESCE(
                        EXCLUDED.thread_message_position, chat_messages.thread_message_position
                    ),
                    sender_id = COALESCE(EXCLUDED.sender_id, chat_messages.sender_id),
                    sender_id_type = COALESCE(EXCLUDED.sender_id_type, chat_messages.sender_id_type),
                    sender_open_id = COALESCE(EXCLUDED.sender_open_id, chat_messages.sender_open_id),
                    sender_tenant_key = COALESCE(EXCLUDED.sender_tenant_key, chat_messages.sender_tenant_key),
                    sender_type = COALESCE(EXCLUDED.sender_type, chat_messages.sender_type),
                    is_deleted = chat_messages.is_deleted OR EXCLUDED.is_deleted,
                    last_observed_at = NOW()
                """,
                (
                    claim.chat_id,
                    message_id,
                    _lark_time(message.get("create_time")),
                    _lark_time(message.get("update_time")),
                    _text(message.get("msg_type")),
                    body_ciphertext,
                    message_body_fingerprint(fingerprint_key, body_content)
                    if body_ciphertext and fingerprint_key else None,
                    relations["root_id"],
                    relations["parent_id"],
                    relations["thread_id"],
                    json.dumps(relations["mentions"]) if relations["mentions"] is not None else None,
                    relations["message_app_link"],
                    relations["message_position"],
                    relations["thread_message_position"],
                    sender_id,
                    sender_id_type,
                    sender_open_id,
                    sender_tenant_key,
                    sender_type,
                    is_deleted,
                ),
            )
            observation = conn.execute(
                """
                INSERT INTO authorization_message_observations (
                    authorization_id, chat_id, message_id, sent_by_author, observed_at
                ) VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (authorization_id, chat_id, message_id) DO UPDATE SET
                    sent_by_author = authorization_message_observations.sent_by_author OR EXCLUDED.sent_by_author,
                    observed_at = NOW()
                RETURNING sent_by_author, (xmax = 0) AS is_new
                """,
                (claim.job.authorization_id, claim.chat_id, message_id, is_own),
            ).fetchone()
            if observation["is_new"]:
                inserted += 1
                own_inserted += int(bool(observation["sent_by_author"]))

        _refresh_tenant_classifications(conn, internal_tenant_keys)

        if claim.mode == "backfill":
            updated = conn.execute(
                """
                UPDATE authorization_chats
                SET backfill_page_token = %s,
                    backfill_complete = %s,
                    last_backfill_at = CASE WHEN %s THEN NOW() ELSE last_backfill_at END,
                    sync_status = CASE WHEN %s THEN 'succeeded' ELSE 'syncing' END,
                    manual_backfill_requested_at = CASE
                        WHEN %s THEN NULL ELSE manual_backfill_requested_at
                    END,
                    manual_backfill_started_at = CASE WHEN %s THEN NULL ELSE manual_backfill_started_at END,
                    last_error = NULL,
                    page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                    page_claimed_at = NULL, page_lease_until = NULL, page_retry_at = NULL
                WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
                  AND page_claim_id = %s AND page_claim_job_id = %s AND page_claim_mode = 'backfill'
                  AND backfill_page_token IS NOT DISTINCT FROM %s
                RETURNING 1
                """,
                (
                    next_page_token,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    claim.job.authorization_id,
                    claim.chat_id,
                    claim.generation,
                    claim.claim_id,
                    claim.job.id,
                    claim.expected_page_token,
                ),
            ).fetchone()
        else:
            updated = conn.execute(
                """
                UPDATE authorization_chats
                SET incremental_page_token = %s,
                    incremental_window_start_at = CASE WHEN %s THEN NULL ELSE incremental_window_start_at END,
                    incremental_window_end_at = CASE WHEN %s THEN NULL ELSE incremental_window_end_at END,
                    last_incremental_at = CASE WHEN %s THEN NOW() ELSE last_incremental_at END,
                    sync_status = CASE WHEN %s THEN 'succeeded' ELSE 'syncing' END,
                    last_error = NULL,
                    page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                    page_claimed_at = NULL, page_lease_until = NULL, page_retry_at = NULL
                WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
                  AND page_claim_id = %s AND page_claim_job_id = %s AND page_claim_mode = 'incremental'
                  AND incremental_page_token IS NOT DISTINCT FROM %s
                RETURNING 1
                """,
                (
                    next_page_token,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    claim.job.authorization_id,
                    claim.chat_id,
                    claim.generation,
                    claim.claim_id,
                    claim.job.id,
                    claim.expected_page_token,
                ),
            ).fetchone()
        if not updated:
            raise StaleSyncJob
        if inserted:
            creator_handle_matching.touch_chats(conn, [claim.chat_id])
            creator_group_matching.touch_chats(conn, [claim.chat_id])
    return PageCommit(True, inserted, own_inserted)


def _mark_chat_not_readable(claim: PageClaim, *, code: int | None, restricted: bool = False) -> None:
    detail = (
        "群开启受限模式，Lark禁止读取消息（231203）"
        if restricted
        else "Lark暂不允许读取此会话消息" + (f"（错误码 {code}）" if code is not None else "")
    )
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        current_job = conn.execute(
            """
            SELECT 1 FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            FOR UPDATE
            """,
            (claim.job.id, claim.job.discovery_generation),
        ).fetchone()
        if not current_job:
            return
        # Keep successful watermarks and cursors; a changed group restriction gets one daily recheck.
        conn.execute(
            """
            UPDATE authorization_chats
            SET sync_status = 'not_readable', last_error = %s,
                page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                page_claimed_at = NULL, page_lease_until = NULL,
                page_retry_at = NOW() + INTERVAL '1 day'
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
              AND page_claim_id = %s AND page_claim_job_id = %s AND page_claim_mode = %s
              AND page_lease_until > NOW()
            """,
            (
                detail,
                claim.job.authorization_id,
                claim.chat_id,
                claim.generation,
                claim.claim_id,
                claim.job.id,
                claim.mode,
            ),
        )


def _release_retryable_page_claim(
    claim: PageClaim,
    *,
    delay_seconds: int,
    detail: str,
    code: int | None = None,
    http_status: int | None = None,
) -> None:
    """Delay one failed page without interrupting sibling conversation reads."""

    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        # Lock parent then page, matching claim/commit lock order.  The exact
        # claim fence makes a late failure from an older retry a no-op.
        job = conn.execute(
            """
            SELECT 1 FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            FOR UPDATE
            """,
            (claim.job.id, claim.job.discovery_generation),
        ).fetchone()
        if not job:
            return
        released = conn.execute(
            """
            UPDATE authorization_chats
            SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                page_claimed_at = NULL, page_lease_until = NULL,
                page_retry_at = NOW() + (%s * INTERVAL '1 second'),
                sync_status = 'failed_retryable', last_error = %s
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
              AND page_claim_id = %s AND page_claim_job_id = %s AND page_claim_mode = %s
              AND page_lease_until > NOW()
            RETURNING 1
            """,
            (
                delay_seconds,
                detail,
                claim.job.authorization_id,
                claim.chat_id,
                claim.generation,
                claim.claim_id,
                claim.job.id,
                claim.mode,
            ),
        ).fetchone()
        if not released:
            return
        conn.execute(
            """
            UPDATE sync_jobs
            SET last_error_code = %s, last_http_status = %s, last_error = %s,
                last_progress_at = NOW(), updated_at = NOW()
            WHERE id = %s AND discovery_generation = %s
            """,
            (code, http_status, detail, claim.job.id, claim.job.discovery_generation),
        )
        _yield_job_to_retry_if_idle(conn, claim.job)


def _yield_job_to_retry_if_idle(conn: psycopg.Connection, job: SyncJob) -> bool:
    """Free a coordinator slot when its only remaining work is delayed."""

    active_claims = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM authorization_chats
        WHERE authorization_id = %s AND page_claim_id IS NOT NULL
          AND page_lease_until > NOW()
        """,
        (job.authorization_id,),
    ).fetchone()
    if int(active_claims["count"]):
        return False
    if _has_claimable_page_in_connection(conn, job):
        return False
    retry = conn.execute(
        """
        SELECT MIN(page_retry_at) AS retry_at
        FROM authorization_chats
        WHERE authorization_id = %s AND page_retry_at > NOW() AND sync_status <> 'not_readable'
        """,
        (job.authorization_id,),
    ).fetchone()
    if not retry["retry_at"]:
        return False
    conn.execute(
        """
        UPDATE sync_jobs
        SET status = 'queued', scheduled_at = %s, lease_until = NULL, updated_at = NOW()
        WHERE id = %s AND status = 'running' AND discovery_generation = %s
        """,
        (retry["retry_at"], job.id, job.discovery_generation),
    )
    return True


def _has_claimable_page_in_connection(conn: psycopg.Connection, job: SyncJob) -> bool:
    """Whether this coordinator has a page it can run now, not after backoff."""

    if _has_pending_backfill_in_connection(conn, job.authorization_id):
        pending = """
            (access.manual_backfill_requested_at IS NOT NULL OR NOT access.backfill_complete)
        """
        params: tuple[object, ...] = (job.authorization_id,)
    else:
        pending = """
            access.backfill_complete
            AND (
                (access.incremental_job_id = %s AND access.incremental_page_token IS NOT NULL)
                OR access.incremental_job_id IS DISTINCT FROM %s
            )
        """
        params = (job.authorization_id, job.id, job.id)
    row = conn.execute(
        f"""
        SELECT EXISTS (
            SELECT 1
            FROM authorization_chats access
            JOIN chats c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
              AND (access.page_claim_id IS NULL OR access.page_lease_until < NOW())
              AND (access.page_retry_at IS NULL OR access.page_retry_at <= NOW())
              AND {pending}
        ) AS ready
        """,
        params,
    ).fetchone()
    return bool(row["ready"])


def _locked_current_page_claim(conn: psycopg.Connection, claim: PageClaim) -> bool:
    """Lock and verify a page claim after its coordinator row is already locked."""

    row = conn.execute(
        """
        SELECT 1
        FROM authorization_chats
        WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
          AND page_claim_id = %s AND page_claim_job_id = %s AND page_claim_mode = %s
          AND page_lease_until > NOW()
        FOR UPDATE
        """,
        (
            claim.job.authorization_id,
            claim.chat_id,
            claim.generation,
            claim.claim_id,
            claim.job.id,
            claim.mode,
        ),
    ).fetchone()
    return bool(row)


def _renew_lease(job: SyncJob) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            """,
            (job.id, job.discovery_generation),
        )


def _add_job_progress(job: SyncJob, *, pages: int, messages: int, own_messages: int) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET pages_processed = pages_processed + %s,
                messages_indexed = messages_indexed + %s,
                own_messages_indexed = own_messages_indexed + %s,
                lease_until = NOW() + INTERVAL '10 minutes',
                last_progress_at = NOW(), last_error_code = NULL, last_http_status = NULL,
                last_error = NULL, updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            """,
            (pages, messages, own_messages, job.id, job.discovery_generation),
        )


def _finish_and_schedule_incremental(job: SyncJob) -> None:
    settings = get_settings()
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        current = conn.execute(
            """
            SELECT mode
            FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            FOR UPDATE
            """,
            (job.id, job.discovery_generation),
        ).fetchone()
        if not current:
            return

        claimed = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM authorization_chats
                WHERE page_claim_job_id = %s AND page_lease_until > NOW()
            ) AS claimed
            """,
            (job.id,),
        ).fetchone()
        if claimed["claimed"] or _has_pending_backfill_in_connection(conn, job.authorization_id):
            return

        # A backfill job owns only backfill cursors.  Once those are complete,
        # finish it and let the new incremental job establish its own windows.
        # Otherwise every uninitialised incremental cursor would keep this
        # backfill coordinator alive forever.
        if current["mode"] == "incremental":
            incremental_pending = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM authorization_chats access
                    JOIN chats c ON c.chat_id = access.chat_id
                    WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
                      AND COALESCE(c.chat_status, 'normal') = 'normal'
                      AND access.backfill_complete
                      AND (access.sync_status <> 'not_readable'
                           OR access.page_retry_at IS NULL OR access.page_retry_at <= NOW())
                      AND (
                        (access.incremental_job_id = %s AND access.incremental_page_token IS NOT NULL)
                        OR access.incremental_job_id IS DISTINCT FROM %s
                      )
                ) AS pending
                """,
                (job.authorization_id, job.id, job.id),
            ).fetchone()
            if incremental_pending["pending"]:
                return

        finished = conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'succeeded', finished_at = NOW(), lease_until = NULL,
                last_error = NULL, updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            RETURNING id
            """,
            (job.id, job.discovery_generation),
        ).fetchone()
        if not finished:
            return
        conn.execute(
            """
            UPDATE authorization_chats
            SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                page_claimed_at = NULL, page_lease_until = NULL,
                sync_status = CASE WHEN sync_status = 'syncing' AND backfill_complete
                    THEN 'succeeded'
                    WHEN sync_status = 'syncing' THEN 'pending'
                    ELSE sync_status
                END
            WHERE page_claim_job_id = %s
            """,
            (job.id,),
        )
        mode = "incremental"
        conn.execute(
            """
            INSERT INTO sync_jobs (id, authorization_id, mode, status, scheduled_at)
            VALUES (
                %s, %s, %s, 'queued',
                CASE WHEN %s = 'backfill' THEN NOW() ELSE NOW() + (%s * INTERVAL '1 second') END
            )
            ON CONFLICT (authorization_id) WHERE status IN ('queued', 'running') DO NOTHING
            """,
            (uuid.uuid4(), job.authorization_id, mode, mode, settings.sync_interval_seconds),
        )


def _requeue(
    job: SyncJob,
    *,
    delay_seconds: int,
    detail: str | None = None,
    code: int | None = None,
    http_status: int | None = None,
    reset_discovery: bool = False,
    claim: PageClaim | None = None,
) -> None:
    """Release all of a job's page claims and retry it from durable cursors."""

    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        current_job = conn.execute(
            """
            SELECT 1 FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            FOR UPDATE
            """,
            (job.id, job.discovery_generation),
        ).fetchone()
        if not current_job:
            return
        if claim and not _locked_current_page_claim(conn, claim):
            return
        row = conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'queued', scheduled_at = NOW() + (%s * INTERVAL '1 second'),
                lease_until = NULL,
                attempts = attempts + 1,
                last_error_code = %s,
                last_http_status = %s,
                last_error = %s,
                discovery_status = CASE WHEN %s THEN 'pending' ELSE discovery_status END,
                discovery_generation = CASE
                    WHEN %s THEN discovery_generation + 1
                    ELSE discovery_generation
                END,
                discovery_completed_at = CASE WHEN %s THEN NULL ELSE discovery_completed_at END,
                updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            RETURNING id
            """,
            (
                delay_seconds,
                code,
                http_status,
                detail,
                reset_discovery,
                reset_discovery,
                reset_discovery,
                job.id,
                job.discovery_generation,
            ),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE authorization_chats
                SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                    page_claimed_at = NULL, page_lease_until = NULL,
                    sync_status = CASE WHEN sync_status = 'syncing' AND backfill_complete
                        THEN 'succeeded'
                        WHEN sync_status = 'syncing' THEN 'pending'
                        ELSE sync_status
                    END
                WHERE page_claim_job_id = %s
                """,
                (job.id,),
            )


def _finish_job(job: SyncJob, status: str, detail: str, code: int | None = None) -> None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        row = conn.execute(
            """
            UPDATE sync_jobs
            SET status = %s, finished_at = NOW(), lease_until = NULL,
                last_error_code = %s, last_error = %s, updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            RETURNING id
            """,
            (status, code, detail, job.id, job.discovery_generation),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE authorization_chats
                SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                    page_claimed_at = NULL, page_lease_until = NULL,
                    sync_status = CASE WHEN sync_status = 'syncing' AND backfill_complete
                        THEN 'succeeded'
                        WHEN sync_status = 'syncing' THEN 'pending'
                        ELSE sync_status
                    END
                WHERE page_claim_job_id = %s
                """,
                (job.id,),
            )


def _mark_attention(job: SyncJob, detail: str, *, claim: PageClaim | None = None) -> None:
    with connection() as conn, conn.transaction():
        lock_sync_material(conn)
        current_job = conn.execute(
            """
            SELECT 1 FROM sync_jobs
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            FOR UPDATE
            """,
            (job.id, job.discovery_generation),
        ).fetchone()
        if not current_job:
            return
        if claim and not _locked_current_page_claim(conn, claim):
            return
        row = conn.execute(
            """
            UPDATE sync_jobs SET status = 'attention_required', finished_at = NOW(), lease_until = NULL,
                last_error = %s, updated_at = NOW()
            WHERE id = %s AND status = 'running' AND discovery_generation = %s
            RETURNING id
            """,
            (detail, job.id, job.discovery_generation),
        ).fetchone()
        if row:
            # Do this only after the job fence succeeds.  A reauthorization
            # increments the generation, so an old request can never turn the
            # freshly authorized account back into an attention state.
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
                UPDATE authorization_chats
                SET page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
                    page_claimed_at = NULL, page_lease_until = NULL,
                    sync_status = CASE WHEN sync_status = 'syncing' AND backfill_complete
                        THEN 'succeeded'
                        WHEN sync_status = 'syncing' THEN 'pending'
                        ELSE sync_status
                    END
                WHERE page_claim_job_id = %s
                """,
                (job.id,),
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


def _body_content(message: dict[str, Any]) -> str | None:
    body = message.get("body")
    return _text(body.get("content")) if isinstance(body, dict) else None


def _message_relation_metadata(message: dict[str, Any]) -> dict[str, object]:
    """Keep only Lark relationship fields that can anchor later evidence."""

    raw_mentions = message.get("mentions")
    mentions: list[dict[str, str]] | None = None
    if isinstance(raw_mentions, list):
        mentions = []
        for mention in raw_mentions:
            if not isinstance(mention, dict):
                continue
            item = {
                key: value
                for key in ("key", "id", "id_type", "name", "tenant_key")
                if (value := _text(mention.get(key)))
            }
            if item:
                mentions.append(item)
    return {
        "root_id": _text(message.get("root_id")),
        "parent_id": _text(message.get("parent_id")),
        "thread_id": _text(message.get("thread_id")),
        # None means the API did not include this field; [] means it did.
        "mentions": mentions,
        "message_app_link": _text(message.get("message_app_link")),
        "message_position": _nonnegative_int(message.get("message_position")),
        "thread_message_position": _nonnegative_int(message.get("thread_message_position")),
    }


def _sender_metadata(message: dict[str, Any]) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return None, None, None, None, None, None
    sender_id = _text(sender.get("id"))
    sender_id_type = _text(sender.get("id_type"))
    return (
        sender_id,
        sender_id_type,
        sender_id if sender_id_type == "open_id" else None,
        _text(sender.get("tenant_key")),
        _text(sender.get("sender_type")),
        _text(sender.get("sender_name")) or _text(sender.get("name")),
    )


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


def _nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _expiry(seconds: int | None) -> datetime | None:
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds else None


if __name__ == "__main__":
    run_forever()

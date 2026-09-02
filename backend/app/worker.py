"""Independent PostgreSQL-backed worker for discovery, backfill, and incremental statistics."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from . import contact_assessments
from .config import Settings, get_settings
from .crypto import TokenCipher, message_body_cipher
from .db import connection
from .lark_api import LarkApiError, LarkClient, TokenBundle

LOGGER = logging.getLogger("lark_message_stats.worker")
MAX_CONSECUTIVE_SYNC_JOBS = 3
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
class MemberSnapshotMeasurement:
    """The latest roster fact and its safe-to-display aggregate."""

    count: int | None
    count_status: str
    snapshot_status: str
    checked_at: datetime | None
    members: tuple[dict[str, str | None], ...] = ()


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx otherwise logs full request URLs, including Lark chat IDs, at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.app_secret_configured:
        LOGGER.warning("Lark App Secret is not configured; worker is waiting for administrator input")

    sync_streak = 0
    while True:
        try:
            _recover_expired_leases()
            # Keep a backfill from starving the durable AI queue forever. The
            # assessment path remains globally rate-limited in PostgreSQL.
            if sync_streak >= MAX_CONSECUTIVE_SYNC_JOBS and _run_contact_assessment(settings):
                sync_streak = 0
                continue
            job = _claim_job() if settings.app_secret_configured else None
            if job:
                _execute_job(job, settings)
                sync_streak += 1
                continue
            if _run_contact_assessment(settings):
                sync_streak = 0
                continue
            sync_streak = 0
            time.sleep(settings.worker_idle_seconds)
        except KeyboardInterrupt:
            raise
        except Exception:  # Unexpected failures must not terminate the systemd worker loop.
            LOGGER.exception("worker loop failed without exposing user data")
            time.sleep(settings.worker_idle_seconds)


def _run_contact_assessment(settings: Settings) -> bool:
    """Run one manually requested assessment without taking down sync processing."""

    try:
        return contact_assessments.run_next_contact_assessment(settings)
    except Exception:
        LOGGER.exception("contact assessment worker failed without exposing user data")
        return False


def _recover_expired_leases() -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'queued', scheduled_at = NOW(), lease_until = NULL,
                attempts = attempts + 1,
                last_error_code = NULL,
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
                lease_until = NOW() + INTERVAL '10 minutes', updated_at = NOW()
            WHERE id = %s
            """,
            (row["id"],),
        )
        return SyncJob(row["id"], row["authorization_id"], row["mode"], int(row["attempts"]))


def _execute_job(job: SyncJob, settings: Settings) -> None:
    try:
        authorization = _authorization(job.authorization_id)
        if not authorization or authorization["status"] != "active":
            _finish_job(job.id, "cancelled", "Authorization is no longer active.")
            return

        client = LarkClient(settings)
        access_token = _usable_access_token(job.authorization_id, authorization, client, settings)
        discovered = _discover_chats(job, authorization, access_token, client, settings, chat_type="group")
        # P2P discovery and content collection are part of the revised OAuth
        # agreement. Existing statistics-only authorizations continue to sync
        # their group metadata until the person explicitly reauthorizes.
        if authorization.get("body_consent_at") and authorization.get("p2p_discovery_status") not in {
            "unavailable",
            "attention_required",
        }:
            discovered += _discover_p2p(job, authorization, access_token, client, settings)
        _set_job_discovered(job.id, discovered)

        # A newly discovered conversation can appear while an incremental job is
        # running. Always drain pending full-history backfills first so those
        # rows never remain at "waiting" indefinitely.
        if job.mode == "backfill" or _has_pending_backfill(job.authorization_id):
            complete = _run_backfill(job, authorization, access_token, client, settings)
        else:
            complete = _run_incremental(job, authorization, access_token, client, settings)

        if complete:
            _finish_and_schedule_incremental(job)
        else:
            _requeue(job.id, delay_seconds=0, retry=False)
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
                retry=True,
            )
        else:
            _finish_job(job.id, "failed", "A Lark API request could not be completed.", exc.lark_code)
    except (psycopg.Error, ValueError):
        _requeue(
            job.id,
            delay_seconds=min(60 * (2 ** min(job.attempts, 4)), 900),
            detail="Temporary service issue; retry scheduled.",
            retry=True,
        )


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
    last_member_snapshot_checks = _member_snapshot_check_times(authorization["id"])
    while True:
        _renew_lease(job.id)
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
            if chat_type == "group" and _member_count_refresh_due(
                last_member_snapshot_checks.get(chat_id), seen_at, settings.member_count_refresh_seconds
            ):
                if "im:chat.members:read" in settings.oauth_scopes:
                    _renew_lease(job.id)
                    measurement = _read_member_snapshot(client, access_token, chat_id)
                else:
                    measurement = MemberSnapshotMeasurement(
                        count=None,
                        count_status="unavailable",
                        snapshot_status="unavailable",
                        checked_at=seen_at,
                    )
                if measurement.checked_at:
                    last_member_snapshot_checks[chat_id] = measurement.checked_at
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
                    (chat_id, name, external, chat_status, chat_mode, seen_at, seen_at, seen_at),
                )
                counterpart_open_id = _p2p_counterpart_open_id(counterpart_id, counterpart_type)
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
                    """
                    INSERT INTO authorization_chats (
                        authorization_id, chat_id, is_visible, last_seen_at,
                        member_count, member_count_status, member_count_checked_at,
                        member_snapshot_status, member_snapshot_checked_at,
                        p2p_counterpart_id, p2p_counterpart_type,
                        p2p_counterpart_open_id, p2p_counterpart_seen_at
                    ) VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        p2p_counterpart_open_id = EXCLUDED.p2p_counterpart_open_id,
                        p2p_counterpart_seen_at = CASE
                            WHEN EXCLUDED.p2p_counterpart_type = 'bot' THEN NULL
                            WHEN EXCLUDED.p2p_counterpart_open_id IS NULL
                                THEN authorization_chats.p2p_counterpart_seen_at
                            ELSE EXCLUDED.p2p_counterpart_seen_at
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
                        counterpart_open_id,
                        seen_at if counterpart_open_id else None,
                    ),
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
        if not _has_more(data):
            break
        page_token = _text(data.get("page_token"))
        if not page_token:
            raise LarkApiError(502, None, "Lark chat pagination did not return a continuation token")

    with connection() as conn, conn.transaction():
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
        _set_p2p_discovery(authorization["id"], status, detail)
        return 0
    _set_p2p_discovery(authorization["id"], "succeeded", None)
    return discovered


def _set_p2p_discovery(authorization_id: uuid.UUID, status: str, detail: str | None) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE authorizations
            SET p2p_discovery_status = %s, p2p_last_checked_at = NOW(),
                p2p_last_error = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (status, detail, authorization_id),
        )


def _member_snapshot_check_times(authorization_id: uuid.UUID) -> dict[str, datetime | None]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, member_snapshot_checked_at
            FROM authorization_chats
            WHERE authorization_id = %s
            """,
            (authorization_id,),
        ).fetchall()
    return {row["chat_id"]: row["member_snapshot_checked_at"] for row in rows}


def _member_count_refresh_due(
    last_checked_at: datetime | None, now: datetime, refresh_seconds: int
) -> bool:
    return last_checked_at is None or last_checked_at <= now - timedelta(seconds=refresh_seconds)


def _p2p_counterpart_open_id(counterpart_id: str | None, counterpart_type: str | None) -> str | None:
    """Only a Lark user target has an open ID suitable for a contact."""

    return counterpart_id if counterpart_type == "user" else None


def _read_member_snapshot(client: LarkClient, access_token: str, chat_id: str) -> MemberSnapshotMeasurement:
    checked_at = datetime.now(UTC)
    page_token: str | None = None
    members: list[dict[str, str | None]] = []
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
        return MemberSnapshotMeasurement(None, status, status, checked_at)

    unique_members = {member["open_id"]: member for member in members}
    total = reported_total if reported_total is not None else len(unique_members) if unique_members else None
    if total is None:
        LOGGER.info("member snapshot response omitted a usable user total")
        return MemberSnapshotMeasurement(None, "unavailable", "unavailable", checked_at)
    snapshot_status = "partial" if partial else "succeeded"
    return MemberSnapshotMeasurement(
        count=total,
        count_status="succeeded",
        snapshot_status=snapshot_status,
        checked_at=checked_at,
        members=tuple(unique_members.values()),
    )


def _member_records(data: dict[str, Any]) -> list[dict[str, str | None]]:
    raw = data.get("users")
    if not isinstance(raw, list):
        raw = data.get("items")
    records: list[dict[str, str | None]] = []
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
                "display_name": _text(member.get("name")) or _text(member.get("en_name")),
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
    for member in measurement.members:
        open_id = member["open_id"]
        assert open_id is not None
        _upsert_lark_identity(
            conn,
            open_id,
            tenant_key=member["tenant_key"],
            display_name=member["display_name"],
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
            display_name=member["display_name"],
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
                member["display_name"],
                member["member_type"] or "user",
                seen_at,
                seen_at,
                authorization_id,
            ),
        )


def _upsert_lark_identity(
    conn: psycopg.Connection,
    open_id: str,
    *,
    tenant_key: str | None = None,
    display_name: str | None = None,
    identity_type: str = "user",
    seen_at: datetime | None = None,
) -> None:
    observed_at = seen_at or datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO lark_identities (
            open_id, tenant_key, display_name, identity_type, first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (open_id) DO UPDATE SET
            tenant_key = COALESCE(EXCLUDED.tenant_key, lark_identities.tenant_key),
            display_name = COALESCE(EXCLUDED.display_name, lark_identities.display_name),
            identity_type = COALESCE(EXCLUDED.identity_type, lark_identities.identity_type),
            last_seen_at = EXCLUDED.last_seen_at
        """,
        (open_id, tenant_key, display_name, identity_type, observed_at, observed_at),
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

    conn.execute(
        """
        UPDATE lark_identities
        SET tenant_classification = CASE
            WHEN identity_type <> 'user' THEN 'internal'
            WHEN tenant_key IS NULL THEN 'unresolved'
            WHEN tenant_key = ANY(%s) THEN 'internal'
            ELSE 'known_external'
        END
        """,
        (list(internal_tenant_keys),),
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
                display_name or identity["display_name"],
                seen_at or datetime.now(UTC),
                seen_at or datetime.now(UTC),
            ),
        )
        conn.execute("UPDATE lark_identities SET contact_id = %s WHERE open_id = %s", (contact_id, open_id))
        return
    if display_name:
        conn.execute(
            """
            UPDATE contacts
            SET display_name = COALESCE(display_name, %s), updated_at = NOW()
            WHERE id = %s
            """,
            (display_name, contact_id),
        )


def _run_backfill(
    job: SyncJob, authorization: dict[str, Any], access_token: str, client: LarkClient, settings: Settings
) -> bool:
    pages = 0
    content_cipher = (
        message_body_cipher(settings.token_encryption_key) if authorization.get("body_consent_at") else None
    )
    while pages < settings.max_pages_per_job:
        chat = _next_backfill_chat(authorization["id"])
        if not chat:
            return True
        now = datetime.now(UTC)
        generation = int(chat["sync_generation"])
        start_at = chat["backfill_window_start_at"]
        end_at = chat["backfill_window_end_at"]
        if not end_at:
            end_at = now
            # A user-requested rescan intentionally ignores the optional
            # default lookback: it means every message Lark can return.
            start_at = (
                None
                if chat["manual_backfill_requested_at"]
                else now - timedelta(days=settings.sync_lookback_days)
                if settings.sync_lookback_days
                else None
            )
            _start_backfill_window(authorization["id"], chat["chat_id"], start_at, end_at, generation)

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
            _mark_chat_not_readable(authorization["id"], chat["chat_id"], generation)
            continue

        next_page_token = _continuation_token(data)

        inserted, own_inserted = _record_page(
            job,
            authorization,
            chat["chat_id"],
            _items(data),
            next_page_token=next_page_token,
            backfill=True,
            generation=generation,
            content_cipher=content_cipher,
        )
        pages += 1
        _add_job_progress(job.id, pages=1, messages=inserted, own_messages=own_inserted)
    return False


def _run_incremental(
    job: SyncJob, authorization: dict[str, Any], access_token: str, client: LarkClient, settings: Settings
) -> bool:
    pages = 0
    content_cipher = (
        message_body_cipher(settings.token_encryption_key) if authorization.get("body_consent_at") else None
    )
    while pages < settings.max_pages_per_job:
        # Finish the current page before yielding to a requested full rescan.
        # The requeued job will claim that request before reading another
        # incremental page for this authorization.
        if _has_manual_backfill_request(authorization["id"]):
            return False
        chat = _next_incremental_chat(authorization["id"], job.id)
        if not chat:
            return True
        generation = int(chat["sync_generation"])
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
            _start_incremental_window(authorization["id"], chat["chat_id"], job.id, start_at, end_at, generation)

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
            _mark_chat_not_readable(authorization["id"], chat["chat_id"], generation)
            continue

        next_page_token = _continuation_token(data)

        inserted, own_inserted = _record_page(
            job,
            authorization,
            chat["chat_id"],
            _items(data),
            next_page_token=next_page_token,
            backfill=False,
            generation=generation,
            content_cipher=content_cipher,
        )
        pages += 1
        _add_job_progress(job.id, pages=1, messages=inserted, own_messages=own_inserted)
    return False


def _next_backfill_chat(authorization_id: uuid.UUID) -> dict[str, Any] | None:
    """Return the next full-history conversation, honoring requested rescans first."""

    with connection() as conn:
        manual_in_progress = conn.execute(
            """
            SELECT access.*
            FROM authorization_chats AS access
            JOIN chats AS c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
              AND access.manual_backfill_requested_at IS NOT NULL
              AND access.manual_backfill_started_at IS NOT NULL
              AND NOT access.backfill_complete
            ORDER BY access.manual_backfill_requested_at, access.chat_id
            LIMIT 1
            """,
            (authorization_id,),
        ).fetchone()
        if manual_in_progress:
            return manual_in_progress

    claimed = _claim_requested_backfill(authorization_id)
    if claimed:
        return claimed

    with connection() as conn:
        return conn.execute(
            """
            SELECT access.*
            FROM authorization_chats AS access
            JOIN chats AS c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal' AND NOT access.backfill_complete
              AND access.sync_status <> 'not_readable'
            ORDER BY access.discovered_at, access.chat_id LIMIT 1
            """,
            (authorization_id,),
        ).fetchone()


def _claim_requested_backfill(authorization_id: uuid.UUID) -> dict[str, Any] | None:
    """Atomically reset and claim one manual full-history rescan request."""

    with connection() as conn, conn.transaction():
        return conn.execute(
            """
            WITH requested_chat AS (
                SELECT access.authorization_id, access.chat_id
                FROM authorization_chats AS access
                JOIN chats AS c ON c.chat_id = access.chat_id
                WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND access.manual_backfill_requested_at IS NOT NULL
                  AND access.manual_backfill_started_at IS NULL
                ORDER BY access.manual_backfill_requested_at, access.chat_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE authorization_chats AS access
            SET backfill_page_token = NULL,
                backfill_complete = FALSE,
                backfill_started_at = NULL,
                backfill_window_start_at = NULL,
                backfill_window_end_at = NULL,
                incremental_page_token = NULL,
                incremental_job_id = NULL,
                incremental_window_start_at = NULL,
                incremental_window_end_at = NULL,
                sync_status = 'pending',
                last_error = NULL,
                manual_backfill_started_at = NOW(),
                sync_generation = access.sync_generation + 1
            FROM requested_chat
            WHERE access.authorization_id = requested_chat.authorization_id
              AND access.chat_id = requested_chat.chat_id
            RETURNING access.*
            """,
            (authorization_id,),
        ).fetchone()


def _has_pending_backfill(authorization_id: uuid.UUID) -> bool:
    with connection() as conn:
        return _has_pending_backfill_in_connection(conn, authorization_id)


def _has_pending_backfill_in_connection(conn: psycopg.Connection, authorization_id: uuid.UUID) -> bool:
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM authorization_chats AS access
            JOIN chats AS c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
              AND (
                    access.manual_backfill_requested_at IS NOT NULL
                 OR (NOT access.backfill_complete AND access.sync_status <> 'not_readable')
              )
        ) AS pending
        """,
        (authorization_id,),
    ).fetchone()
    return bool(row["pending"])


def _has_manual_backfill_request(authorization_id: uuid.UUID) -> bool:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM authorization_chats AS access
                JOIN chats AS c ON c.chat_id = access.chat_id
                WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
                  AND COALESCE(c.chat_status, 'normal') = 'normal'
                  AND access.manual_backfill_requested_at IS NOT NULL
            ) AS pending
            """,
            (authorization_id,),
        ).fetchone()
    return bool(row["pending"])


def _next_incremental_chat(authorization_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            """
            SELECT access.*
            FROM authorization_chats AS access
            JOIN chats AS c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.sync_enabled AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal' AND access.backfill_complete
              AND access.sync_status <> 'not_readable'
              AND (
                    (access.incremental_job_id = %s AND access.incremental_page_token IS NOT NULL)
                 OR access.incremental_job_id IS DISTINCT FROM %s
              )
            ORDER BY
                CASE
                    WHEN access.incremental_job_id = %s AND access.incremental_page_token IS NOT NULL THEN 0
                    ELSE 1
                END,
                COALESCE(access.last_incremental_at, access.last_backfill_at), access.chat_id
            LIMIT 1
            """,
            (authorization_id, job_id, job_id, job_id),
        ).fetchone()


def _start_backfill_window(
    authorization_id: uuid.UUID, chat_id: str, start_at: datetime | None, end_at: datetime, generation: int
) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE authorization_chats
            SET backfill_started_at = COALESCE(backfill_started_at, NOW()),
                backfill_window_start_at = COALESCE(backfill_window_start_at, %s),
                backfill_window_end_at = COALESCE(backfill_window_end_at, %s),
                sync_status = 'syncing', last_error = NULL
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
            """,
            (start_at, end_at, authorization_id, chat_id, generation),
        )


def _start_incremental_window(
    authorization_id: uuid.UUID,
    chat_id: str,
    job_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    generation: int,
) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE authorization_chats
            SET incremental_job_id = %s,
                incremental_window_start_at = COALESCE(incremental_window_start_at, %s),
                incremental_window_end_at = COALESCE(incremental_window_end_at, %s),
                sync_status = 'syncing', last_error = NULL
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
            """,
            (job_id, start_at, end_at, authorization_id, chat_id, generation),
        )


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


def _record_page(
    job: SyncJob,
    authorization: dict[str, Any],
    chat_id: str,
    messages: list[dict[str, Any]],
    *,
    next_page_token: str | None,
    backfill: bool,
    generation: int,
    content_cipher: TokenCipher | None,
) -> tuple[int, int]:
    inserted = 0
    own_inserted = 0
    with connection() as conn, conn.transaction():
        internal_tenant_keys = _internal_tenant_keys(conn)
        for message in messages:
            message_id = _text(message.get("message_id"))
            if not message_id:
                continue
            is_own = _is_author_message(message, authorization["open_id"])
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
                    message_type, body_ciphertext,
                    root_id, parent_id, thread_id, mentions_json,
                    message_app_link, message_position, thread_message_position,
                    sender_id, sender_id_type, sender_open_id, sender_tenant_key, sender_type,
                    is_deleted, first_indexed_at, last_observed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                    chat_id,
                    message_id,
                    _lark_time(message.get("create_time")),
                    _lark_time(message.get("update_time")),
                    _text(message.get("msg_type")),
                    body_ciphertext,
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
                (authorization["id"], chat_id, message_id, is_own),
            ).fetchone()
            if observation["is_new"]:
                inserted += 1
                own_inserted += int(bool(observation["sent_by_author"]))

        _refresh_tenant_classifications(conn, internal_tenant_keys)

        if backfill:
            conn.execute(
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
                    last_error = NULL
                WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
                """,
                (
                    next_page_token,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    authorization["id"],
                    chat_id,
                    generation,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE authorization_chats
                SET incremental_page_token = %s,
                    incremental_window_start_at = CASE WHEN %s THEN NULL ELSE incremental_window_start_at END,
                    incremental_window_end_at = CASE WHEN %s THEN NULL ELSE incremental_window_end_at END,
                    last_incremental_at = CASE WHEN %s THEN NOW() ELSE last_incremental_at END,
                    sync_status = CASE WHEN %s THEN 'succeeded' ELSE 'syncing' END,
                    last_error = NULL
                WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
                """,
                (
                    next_page_token,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    next_page_token is None,
                    authorization["id"],
                    chat_id,
                    generation,
                ),
            )
    return inserted, own_inserted


def _mark_chat_not_readable(authorization_id: uuid.UUID, chat_id: str, generation: int) -> None:
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE authorization_chats
            SET sync_status = 'not_readable', backfill_complete = TRUE,
                incremental_page_token = NULL,
                manual_backfill_requested_at = NULL,
                manual_backfill_started_at = NULL,
                last_error = 'This conversation is not readable with the current authorization.'
            WHERE authorization_id = %s AND chat_id = %s AND sync_generation = %s
            """,
            (authorization_id, chat_id, generation),
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
        mode = "backfill" if _has_pending_backfill_in_connection(conn, job.authorization_id) else "incremental"
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
    job_id: uuid.UUID,
    *,
    delay_seconds: int,
    retry: bool,
    detail: str | None = None,
    code: int | None = None,
) -> None:
    """Put a job back on the queue without treating normal pagination as an error."""

    error_code = code if retry else None
    error_detail = detail if retry else None
    with connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE sync_jobs
            SET status = 'queued', scheduled_at = NOW() + (%s * INTERVAL '1 second'),
                lease_until = NULL,
                attempts = attempts + CASE WHEN %s THEN 1 ELSE 0 END,
                last_error_code = %s,
                last_error = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (delay_seconds, retry, error_code, error_detail, job_id),
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

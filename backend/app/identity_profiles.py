"""Daily, low-priority profile cache; permission failures never interrupt IM sync."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .crypto import TokenCipher
from .db import connection
from .identity_names import update_identity_names
from .lark_api import LarkApiError

LOGGER = logging.getLogger("lark_message_stats.identity_profiles")
MAX_WORKERS = 2
DAILY_SECONDS = 86_400
LEASE_SECONDS = 300
SCOPE_ERRORS = {99991672, 99991679}


def safe_avatar_url(value) -> str | None:
    """Only direct HTTPS Lark CDN images; never proxy or fetch arbitrary URLs."""
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip():
        return None
    if any(ord(character) < 33 for character in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or not host.endswith(".feishucdn.com")
                or host.startswith(".") or parsed.fragment):
            return None
    except ValueError:
        return None
    return value


def store_profile_avatar(conn, open_id, url, avatar_present=True) -> bool:
    """OAuth and background refresh share the same safe, identity-bound write."""
    if not avatar_present:
        return False
    safe = safe_avatar_url(url) if url else None
    if url and safe is None:
        conn.execute(
            """UPDATE lark_identities SET avatar_status='unavailable',avatar_error_code='unsafe_avatar_url',
               avatar_checked_at=NOW(),avatar_retry_at=NOW()+INTERVAL '1 day',avatar_claim_id=NULL
               WHERE open_id=%s AND identity_type='user'""", (open_id,),
        )
        return False
    conn.execute(
        """UPDATE lark_identities SET avatar_url=%s,
           avatar_updated_at=CASE WHEN avatar_url IS DISTINCT FROM %s THEN NOW() ELSE avatar_updated_at END,
           avatar_checked_at=NOW(),avatar_retry_at=NOW()+INTERVAL '1 day',
           avatar_status='succeeded',avatar_error_code=NULL,avatar_claim_id=NULL
           WHERE open_id=%s AND identity_type='user'""", (safe, safe, open_id),
    )
    return True


def claim_next():
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """SELECT identity.open_id FROM lark_identities identity
               WHERE identity.identity_type='user' AND identity.avatar_retry_at<=NOW()
               ORDER BY EXISTS(SELECT 1 FROM authorizations auth WHERE auth.open_id=identity.open_id
                               AND auth.status='active') DESC,
                        identity.avatar_checked_at NULLS FIRST,identity.open_id
               FOR UPDATE OF identity SKIP LOCKED LIMIT 1""",
        ).fetchone()
        if not row:
            return None
        return conn.execute(
            """UPDATE lark_identities SET avatar_claim_id=%s,
               avatar_status=CASE WHEN avatar_status='unavailable' THEN avatar_status ELSE 'running' END,
               avatar_retry_at=NOW()+(%s*INTERVAL '1 second') WHERE open_id=%s
               RETURNING open_id,avatar_claim_id""", (uuid4(), LEASE_SECONDS, row["open_id"]),
        ).fetchone()


_VISIBLE = """access.is_visible AND access.sync_enabled AND COALESCE(chat.chat_status,'normal')='normal'
    AND auth.body_consent_at IS NOT NULL
    AND ((access.p2p_counterpart_open_id=%s AND access.p2p_counterpart_type='user')
      OR EXISTS(SELECT 1 FROM chat_members member WHERE member.chat_id=access.chat_id AND member.open_id=%s
                AND member.member_type='user' AND member.last_observed_by_authorization_id=access.authorization_id)
      OR EXISTS(SELECT 1 FROM authorization_message_observations observation
                JOIN chat_messages message USING(chat_id,message_id)
                WHERE observation.authorization_id=access.authorization_id AND observation.chat_id=access.chat_id
                  AND observation.observed_at>=auth.body_consent_at
                  AND message.sender_open_id=%s AND NOT message.is_deleted))"""


def _source(conn, open_id, excluded=()):
    # Membership/message metadata locates a permitted CM; no message bodies are read.
    return conn.execute(
        """SELECT auth.id,auth.open_id,auth.access_token_ciphertext,auth.scopes,auth.body_consent_at,
                  scope.chat_id,scope.sync_generation
           FROM authorizations auth
           LEFT JOIN identity_profile_providers provider ON provider.authorization_id=auth.id
           LEFT JOIN LATERAL (
             SELECT access.chat_id,access.sync_generation FROM authorization_chats access
             JOIN chats chat USING(chat_id)
             WHERE access.authorization_id=auth.id AND """ + _VISIBLE + """
             ORDER BY access.chat_id LIMIT 1
           ) scope ON TRUE
           WHERE auth.status='active' AND auth.id<>ALL(%s::uuid[])
             AND (auth.open_id=%s OR scope.chat_id IS NOT NULL)
           ORDER BY (auth.open_id=%s) DESC,
                    COALESCE(provider.retry_at>NOW() AND provider.scopes=auth.scopes,FALSE) ASC,
                    (provider.status='available') DESC NULLS LAST,
                    auth.id
           LIMIT 1""", (open_id, open_id, open_id, list(excluded), open_id, open_id),
    ).fetchone()


def _scope_current(conn, source, open_id, *, lock=False):
    current = conn.execute(
        """SELECT open_id,access_token_ciphertext,scopes,body_consent_at
           FROM authorizations WHERE id=%s AND status='active'"""
        + (" FOR SHARE" if lock else ""), (source["id"],),
    ).fetchone()
    if (not current or current["open_id"] != source["open_id"]
            or current["access_token_ciphertext"] != source["access_token_ciphertext"]
            or current["scopes"] != source["scopes"]):
        return False
    if current["open_id"] == open_id:
        return True
    if current["body_consent_at"] != source["body_consent_at"]:
        return False
    access = conn.execute(
        """SELECT access.chat_id,access.p2p_counterpart_type,access.p2p_counterpart_open_id
           FROM authorization_chats access JOIN chats chat USING(chat_id)
           JOIN authorizations auth ON auth.id=access.authorization_id
           WHERE access.authorization_id=%s AND access.chat_id=%s AND access.sync_generation=%s AND """
        + _VISIBLE + (" FOR SHARE OF access,chat" if lock else ""),
        (source["id"], source["chat_id"], source["sync_generation"], open_id, open_id, open_id),
    ).fetchone()
    if not access or not lock:
        return bool(access)
    # Match sync's auth/access/identity order, then lock one actual visibility proof.
    if not conn.execute("SELECT 1 FROM lark_identities WHERE open_id=%s AND identity_type='user' FOR UPDATE",
                        (open_id,)).fetchone():
        return False
    if access["p2p_counterpart_type"] == "user" and access["p2p_counterpart_open_id"] == open_id:
        return True
    if conn.execute(
        """SELECT 1 FROM chat_members WHERE chat_id=%s AND open_id=%s AND member_type='user'
           AND last_observed_by_authorization_id=%s FOR SHARE""", (source["chat_id"], open_id, source["id"]),
    ).fetchone():
        return True
    return bool(conn.execute(
        """SELECT 1 FROM authorization_message_observations observation
           JOIN chat_messages message USING(chat_id,message_id)
           WHERE observation.authorization_id=%s AND observation.chat_id=%s
             AND observation.observed_at>=%s AND message.sender_open_id=%s AND NOT message.is_deleted
           LIMIT 1 FOR SHARE OF observation,message""",
        (source["id"], source["chat_id"], source["body_consent_at"], open_id),
    ).fetchone())


def _provider_gate(source):
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """INSERT INTO identity_profile_providers(authorization_id,scopes) VALUES(%s,%s)
               ON CONFLICT(authorization_id) DO UPDATE SET
                 status=CASE WHEN identity_profile_providers.scopes<>EXCLUDED.scopes
                     THEN 'pending' ELSE identity_profile_providers.status END,
                 retry_at=CASE WHEN identity_profile_providers.scopes<>EXCLUDED.scopes
                     THEN NOW() ELSE identity_profile_providers.retry_at END,
                 scopes=EXCLUDED.scopes RETURNING *,retry_at>NOW() AS cooling""",
            (source["id"], source["scopes"]),
        ).fetchone()
        if row["status"] == "available":
            return None
        if row["cooling"]:
            return row
        # Exactly one probe per CM may cross a denied/missing-field capability boundary.
        conn.execute(
            """UPDATE identity_profile_providers SET status='checking',
               retry_at=NOW()+(%s*INTERVAL '1 second') WHERE authorization_id=%s""",
            (LEASE_SECONDS, source["id"]),
        )
    return None


def _wait_avatar_slot():
    while True:
        with connection() as conn, conn.transaction():
            row = conn.execute(
                """SELECT EXTRACT(EPOCH FROM avatar_next_request_at-clock_timestamp()) AS wait
                   FROM lark_sync_admission_state WHERE id FOR UPDATE""",
            ).fetchone()
            wait = max(0, float(row["wait"]))
            if not wait:
                conn.execute("UPDATE lark_sync_admission_state SET "
                             "avatar_next_request_at=clock_timestamp()+INTERVAL '1 second' WHERE id")
                return
        time.sleep(min(wait, 1))


def _outcome(conn, job, status, code, *, retry_seconds=DAILY_SECONDS, retry_at=None):
    conn.execute(
        """UPDATE lark_identities SET avatar_status=%s,avatar_error_code=%s,avatar_checked_at=NOW(),
           avatar_retry_at=COALESCE(%s,NOW()+(%s*INTERVAL '1 second')),avatar_claim_id=NULL
           WHERE open_id=%s AND avatar_claim_id=%s""",
        (status, code, retry_at, retry_seconds, job["open_id"], job["avatar_claim_id"]),
    )


def _local_outcome(job, status, code, **kwargs):
    with connection() as conn, conn.transaction():
        _outcome(conn, job, status, code, **kwargs)


def _provider_result(conn, source, status, code, seconds):
    conn.execute(
        """UPDATE identity_profile_providers SET status=%s,error_code=%s,checked_at=NOW(),
           retry_at=NOW()+(%s*INTERVAL '1 second') WHERE authorization_id=%s AND scopes=%s""",
        (status, code, seconds, source["id"], source["scopes"]),
    )


def _skip_denied_directory():
    """A known app-wide gap is one local update, not 7,000 forbidden HTTP requests."""
    with connection() as conn, conn.transaction():
        conn.execute(
            """WITH visible AS (
                 SELECT member.open_id,auth.id,auth.scopes FROM authorizations auth
                 JOIN authorization_chats access ON access.authorization_id=auth.id
                 JOIN chats chat USING(chat_id) JOIN chat_members member USING(chat_id)
                 WHERE auth.status='active' AND auth.body_consent_at IS NOT NULL
                   AND access.is_visible AND access.sync_enabled AND COALESCE(chat.chat_status,'normal')='normal'
                   AND member.member_type='user' AND member.last_observed_by_authorization_id=auth.id
                 UNION
                 SELECT access.p2p_counterpart_open_id,auth.id,auth.scopes FROM authorizations auth
                 JOIN authorization_chats access ON access.authorization_id=auth.id JOIN chats chat USING(chat_id)
                 WHERE auth.status='active' AND auth.body_consent_at IS NOT NULL
                   AND access.is_visible AND access.sync_enabled AND COALESCE(chat.chat_status,'normal')='normal'
                   AND access.p2p_counterpart_type='user' AND access.p2p_counterpart_open_id IS NOT NULL
                 UNION
                 SELECT message.sender_open_id,auth.id,auth.scopes FROM authorizations auth
                 JOIN authorization_chats access ON access.authorization_id=auth.id JOIN chats chat USING(chat_id)
                 JOIN authorization_message_observations observation
                   ON observation.authorization_id=auth.id AND observation.chat_id=access.chat_id
                 JOIN chat_messages message ON message.chat_id=observation.chat_id
                   AND message.message_id=observation.message_id
                 WHERE auth.status='active' AND auth.body_consent_at IS NOT NULL
                   AND access.is_visible AND access.sync_enabled AND COALESCE(chat.chat_status,'normal')='normal'
                   AND observation.observed_at>=auth.body_consent_at AND NOT message.is_deleted
               ), gaps AS (
                 SELECT identity.open_id,COUNT(visible.id) AS sources,
                   BOOL_AND(COALESCE(provider.status='unavailable' AND provider.retry_at>NOW()
                            AND provider.scopes=visible.scopes,FALSE)) AS blocked,
                   MIN(provider.retry_at) AS retry_at
                 FROM lark_identities identity LEFT JOIN visible ON visible.open_id=identity.open_id
                 LEFT JOIN identity_profile_providers provider ON provider.authorization_id=visible.id
                 WHERE identity.identity_type='user'
                   AND (identity.avatar_retry_at<=NOW() OR identity.avatar_error_code='profile_probe_pending')
                   AND NOT EXISTS(SELECT 1 FROM authorizations auth
                                  WHERE auth.open_id=identity.open_id AND auth.status='active')
                 GROUP BY identity.open_id
               )
               UPDATE lark_identities identity SET avatar_status='unavailable',
                 avatar_error_code=CASE WHEN gaps.sources=0 THEN 'no_visible_authorization'
                                       ELSE 'profile_scope_unavailable' END,
                 avatar_checked_at=NOW(),avatar_retry_at=COALESCE(gaps.retry_at,NOW()+INTERVAL '1 day'),
                 avatar_claim_id=NULL FROM gaps WHERE identity.open_id=gaps.open_id
                 AND (gaps.sources=0 OR gaps.blocked)
                 AND (identity.avatar_retry_at<=NOW() OR identity.avatar_error_code='profile_probe_pending')""",
        )


def _finish_response(job, source, profile=None, error=None, *, defer_unavailable=False):
    provider_denied = False
    with connection() as conn, conn.transaction():
        if not _scope_current(conn, source, job["open_id"], lock=True):
            _outcome(conn, job, "unavailable", "authorization_changed")
            return False
        if not conn.execute(
            "SELECT 1 FROM lark_identities WHERE open_id=%s AND avatar_claim_id=%s FOR UPDATE",
            (job["open_id"], job["avatar_claim_id"]),
        ).fetchone():
            return False
        own = source["open_id"] == job["open_id"]
        if error:
            denied = error.lark_code in SCOPE_ERRORS
            unavailable = denied or error.likely_auth_problem or error.lark_code == 41050
            code = "profile_scope_unavailable" if denied else f"lark_{error.status_code}_{error.lark_code or 0}"
            delay = DAILY_SECONDS if unavailable else max(300, error.retry_after or 0)
            if not defer_unavailable:
                _outcome(conn, job, "unavailable" if unavailable else "failed", code, retry_seconds=delay)
            if not own and (denied or not unavailable):
                _provider_result(conn, source, "unavailable" if denied else "failed", code, delay)
                provider_denied = denied
        elif profile.open_id != job["open_id"]:
            _outcome(conn, job, "unavailable", "identity_mismatch")
        else:
            if not own and not conn.execute(
                "SELECT 1 FROM authorizations WHERE open_id=%s", (job["open_id"],),
            ).fetchone():
                update_identity_names(
                    conn, job["open_id"], name=profile.name, en_name=profile.en_name,
                    en_name_present=profile.en_name_present, authoritative=True,
                )
            if not profile.avatar_present:
                if not defer_unavailable:
                    _outcome(conn, job, "unavailable", "avatar_field_unavailable")
                if not own:
                    _provider_result(conn, source, "unavailable", "avatar_field_unavailable", DAILY_SECONDS)
                    provider_denied = True
            else:
                store_profile_avatar(conn, job["open_id"], profile.avatar_url)
                if not own:
                    _provider_result(conn, source, "available", None, 0)
    if provider_denied:
        _skip_denied_directory()
    return True


def run_claimed(job, client, settings):
    # Local import keeps the existing token-rotation guard without a worker/module cycle.
    from .worker import ReauthorizationRequired, _authorization, _usable_access_token

    try:
        excluded, status, code, retry_at = [], "unavailable", "no_visible_authorization", None
        # ponytail: at most three existing, independently permitted CM sources; no CM×user scan matrix.
        for _ in range(3):
            with connection() as conn:
                source = _source(conn, job["open_id"], excluded)
            if not source:
                break
            excluded.append(source["id"])
            own = source["open_id"] == job["open_id"]
            blocked = None if own else _provider_gate(source)
            if blocked:
                status = "failed" if blocked["status"] == "failed" else "unavailable"
                code, retry_at = blocked["error_code"] or "profile_probe_pending", blocked["retry_at"]
                continue
            authorization = _authorization(source["id"])
            if not authorization or authorization["status"] != "active":
                raise ReauthorizationRequired
            token = _usable_access_token(source["id"], authorization, client, settings)
            with connection() as conn:
                current = conn.execute("SELECT access_token_ciphertext FROM authorizations WHERE id=%s",
                                       (source["id"],)).fetchone()
                if (not current or TokenCipher(settings.token_encryption_key).decrypt(
                        current["access_token_ciphertext"]) != token):
                    raise ReauthorizationRequired
                source = {**dict(source), "access_token_ciphertext": current["access_token_ciphertext"]}
            _wait_avatar_slot()
            with connection() as conn:
                if not _scope_current(conn, source, job["open_id"]) or not conn.execute(
                    "SELECT 1 FROM lark_identities WHERE open_id=%s AND avatar_claim_id=%s",
                    (job["open_id"], job["avatar_claim_id"]),
                ).fetchone():
                    _local_outcome(job, "unavailable", "authorization_changed")
                    return
            try:
                profile = client.user_info(token) if own else client.contact_user(token, job["open_id"])
            except LarkApiError as error:
                another_source = not own and (error.lark_code in SCOPE_ERRORS | {41050} or error.status_code == 403)
                if not _finish_response(job, source, error=error, defer_unavailable=another_source):
                    return
                if not another_source:
                    return
                status, retry_at = "unavailable", None
                code = "profile_scope_unavailable" if error.lark_code in SCOPE_ERRORS else "contact_not_visible"
            else:
                another_source = not own and not profile.avatar_present
                if not _finish_response(job, source, profile=profile, defer_unavailable=another_source):
                    return
                if not another_source:
                    return
                status, code, retry_at = "unavailable", "avatar_field_unavailable", None
        _local_outcome(job, status, code, retry_at=retry_at)
        if code == "no_visible_authorization":
            _skip_denied_directory()
    except ReauthorizationRequired:
        _local_outcome(job, "unavailable", "authorization_unavailable")
    except Exception:
        LOGGER.warning("optional avatar refresh incomplete; message sync continues")
        _local_outcome(job, "failed", "temporary_failure", retry_seconds=300)


def fill_slots(settings, client, executor, futures):
    """Only short claims/reaping on the worker thread; no network or waiting here."""
    for future in tuple(futures):
        if future.done():
            futures.remove(future)
            try:
                future.result()
            except Exception:
                LOGGER.warning("optional avatar worker failed; its short lease will recover")
    while len(futures) < MAX_WORKERS:
        job = claim_next()
        if not job:
            break
        futures.add(executor.submit(run_claimed, job, client, settings))

"""FastAPI Web app for Lark OAuth and the per-user message statistics dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    admin,
    admin_permissions,
    assistant,
    assistant_routes,
    creator_access,
    creator_broadcast,
    creator_group_routes,
    creator_handle_matching_routes,
    creator_handles_routes,
    creator_responsibility_routes,
    desktop_auth_routes,
    desktop_reply_routes,
    offhours_summary_routes,
)
from .admin_reporting import sync_summary
from .config import get_settings
from .crypto import TokenCipher
from .db import connection, database_is_ready, lock_sync_material
from .identity_names import update_identity_names
from .identity_profiles import safe_avatar_url, store_profile_avatar
from .lark_api import LarkApiError, LarkClient

APP_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

SESSION_COOKIE = "__Host-lark_message_stats"
OAUTH_STATE_COOKIE = "__Host-lark_oauth_state"
SESSION_DAYS = 30
PROFILE_SCOPE_UPGRADE_REASON = "profile_scope_upgrade_required"
SEND_OAUTH_SCOPES = ("im:message", "im:message.send_as_user")
LONDON_TIME_ZONE = ZoneInfo("Europe/London")
LOGGER = logging.getLogger(__name__)
REMI_TEST_OWNER_ID = uuid.UUID("9d5f4370-6463-4c3a-a2ed-67753a1cabc0")
REMI_TEST_SHANG_MA_ID = uuid.UUID("1225b4a0-fbd8-431e-962e-25e66943a8a0")


def _template(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
    if name in {"dashboard.html", "creator_handles.html", "creator_broadcast.html", "offhours_summary.html"}:
        authorization = context["authorization"]
        # Reuse the page's effective access; a navigation link never grants access.
        owner = bool(context.get("admin_role") == "owner"
                     or context.get("creator_data", {}).get("can_view_unassigned")
                     or context.get("summary_data", {}).get("can_manage")
                     or context.get("broadcast_data", {}).get("can_select_cm"))
        active = authorization["status"] == "active"
        context = {**context, "business_nav": {
            "can_broadcast": active and (owner or bool(authorization.get("broadcast_enabled"))
                                         or (bool(authorization.get("body_consent_at"))
                                             and creator_broadcast.preview_open())),
            "can_summary": context.get("offhours_summary_visible", active and (
                bool(authorization.get("body_consent_at")) or owner
                or _is_remi_tester(authorization, {"is_owner": owner})
            )),
        }}
        with connection() as conn:
            assistant_configured = assistant.configured(conn)
        context.update(
            assistant_enabled=active and context['business_nav']['can_summary'],
            assistant_configured=assistant_configured,
            assistant_user_id=str(authorization['id']),
            csrf_token=_admin_csrf_token(authorization),
        )
        selected_cm = (context.get('summary_data', {}).get('responsible', {}).get('id')
                       or context.get('creator_data', {}).get('selected_cm_id')
                       or context.get('broadcast_data', {}).get('selected_cm_id'))
        if selected_cm == 'all':
            selected_cm = authorization['id']
        context['assistant_context'] = {'cm_id': str(selected_cm)} if selected_cm else {}
    return templates.TemplateResponse(request, name, context, status_code=status_code)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    if not database_is_ready():
        raise HTTPException(status_code=503, detail="database unavailable")
    return {
        "status": "ok",
        "database": "ok",
        "mode": "lark-message-stats",
        "lark_secret": "configured" if get_settings().app_secret_configured else "awaiting-admin-input",
    }


@app.get("/")
def index(request: Request):
    authorization = _current_authorization(request)
    if authorization:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/auth/lark/start", status_code=303)


def _oauth_database_unavailable(
    request, error, request_id, *, stage, authorization_id=None, send_authorization_id=None,
):
    # Never log SQL, provider error text, OAuth codes, cookies or token values.
    LOGGER.error(
        "oauth_database_failed request_id=%s authorization_id=%s stage=%s sqlstate=%s",
        request_id, authorization_id, stage, error.sqlstate,
    )
    return_url = "/auth/lark/start"
    if send_authorization_id:
        return_url += f"?send=1&account_id={send_authorization_id}"
    response = _template(
        request, "error.html",
        {"title": "授权暂未完成", "detail": f"请重新发起授权。问题编号：{request_id}",
         "return_url": return_url, "return_label": "重新授权"}, 503,
    )
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


@app.get("/auth/lark/start")
def start_lark_authorization(request: Request, send: bool = False, account_id: str | None = None):
    request_id = uuid.uuid4().hex
    settings = get_settings()
    if not settings.app_secret_configured:
        return _template(
            request,
            "error.html",
            {"title": "服务尚未配置", "detail": "管理员尚未在服务器完成 App Secret 的安全录入。"},
            status_code=503,
        )

    try:
        authorization = _current_authorization(request) if send else None
    except psycopg.Error as error:
        return _oauth_database_unavailable(request, error, request_id, stage="start_session")
    if send:
        if (not authorization or authorization["status"] not in {"active", "attention_required"}
                or not authorization["body_consent_at"]):
            return _template(
                request, "error.html", {"title": "无法增加发送权限", "detail": "请使用已授权的本人账号登录。"}, 403,
            )
        if account_id is not None and account_id != str(authorization["id"]):
            return _template(
                request, "error.html",
                {"title": "发送授权账号不一致", "detail": "请目标人员使用本人账号登录后授权。"}, 403,
            )
        settings = replace(settings, oauth_scopes=tuple(dict.fromkeys((
            *settings.oauth_scopes, *(authorization.get("scopes") or ()), *SEND_OAUTH_SCOPES,
        ))))

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _code_challenge(verifier)
    cipher = TokenCipher(settings.token_encryption_key)

    try:
        with connection() as conn, conn.transaction():
            conn.execute("DELETE FROM oauth_states WHERE expires_at <= NOW()")
            conn.execute(
                """
                INSERT INTO oauth_states (
                    state_hash, code_verifier_ciphertext, expires_at, requested_scopes, authorization_id
                ) VALUES (%s, %s, NOW() + INTERVAL '10 minutes', %s, %s)
                """,
                (_sha256(state), cipher.encrypt(verifier), list(settings.oauth_scopes),
                 authorization["id"] if send else None),
            )
    except psycopg.Error as error:
        return _oauth_database_unavailable(
            request, error, request_id, stage="start_state",
            authorization_id=authorization["id"] if send else None,
            send_authorization_id=authorization["id"] if send else None,
        )

    destination = LarkClient(settings).authorization_url(state, challenge)
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=10 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@app.get("/auth/lark/callback")
def lark_callback(request: Request, code: str | None = None, state: str | None = None):
    request_id = uuid.uuid4().hex
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not code or not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return _template(request, "error.html", {"title": "授权校验失败", "detail": "请返回首页重新开始授权。"}, 400)

    settings = get_settings()
    if not settings.app_secret_configured:
        return _template(request, "error.html", {"title": "服务尚未配置", "detail": "请联系管理员完成安全录入。"}, 503)

    cipher = TokenCipher(settings.token_encryption_key)
    try:
        with connection() as conn, conn.transaction():
            state_row = conn.execute(
                """
                DELETE FROM oauth_states
                WHERE state_hash = %s AND expires_at > NOW()
                RETURNING code_verifier_ciphertext, created_at, requested_scopes, authorization_id
                """,
                (_sha256(state),),
            ).fetchone()
    except psycopg.Error as error:
        return _oauth_database_unavailable(request, error, request_id, stage="consume_state")
    if not state_row:
        return _template(request, "error.html", {"title": "授权已过期", "detail": "请返回首页重新开始授权。"}, 400)

    base_required_scopes = settings.oauth_scopes
    send_upgrade = bool(state_row["authorization_id"])
    requested_scopes = tuple(state_row["requested_scopes"] or settings.oauth_scopes)
    if send_upgrade and not set(SEND_OAUTH_SCOPES).issubset(requested_scopes):
        return _template(request, "error.html", {"title": "授权校验失败", "detail": "请重新开始发送权限授权。"}, 400)
    if send_upgrade:
        try:
            authorization = _current_authorization(request)
        except psycopg.Error as error:
            return _oauth_database_unavailable(
                request, error, request_id, stage="callback_session",
                authorization_id=state_row["authorization_id"],
                send_authorization_id=state_row["authorization_id"],
            )
        if (not authorization or authorization["id"] != state_row["authorization_id"]
                or authorization["status"] not in {"active", "attention_required"}
                or not authorization["body_consent_at"]):
            return _template(
                request, "error.html",
                {"title": "发送授权账号不一致", "detail": "请使用发起增权的本人账号登录后重新授权，原有授权保持不变。"},
                403,
            )
    if requested_scopes != settings.oauth_scopes:
        settings = replace(settings, oauth_scopes=requested_scopes)

    stage = "code_verifier"
    try:
        verifier = cipher.decrypt(state_row["code_verifier_ciphertext"])
        lark = LarkClient(settings)
        stage = "token_exchange"
        tokens = lark.exchange_code(code, verifier)
        stage = "user_info"
        profile = lark.user_info(tokens.access_token)
    except (LarkApiError, InvalidTag, ValueError, RuntimeError) as error:
        LOGGER.warning(
            "oauth_provider_failed request_id=%s authorization_id=%s stage=%s lark_code=%s",
            request_id, state_row["authorization_id"], stage,
            error.lark_code if isinstance(error, LarkApiError) else None,
        )
        return_url = "/auth/lark/start"
        if send_upgrade:
            return_url += f"?send=1&account_id={state_row['authorization_id']}"
        response = _template(
            request,
            "error.html",
            {"title": "Lark 授权未完成", "detail": f"请重新发起授权。问题编号：{request_id}",
             "return_url": return_url, "return_label": "重新授权"},
            502,
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
        return response

    authorization_id = uuid.uuid4()
    token_expires_at = _expiry(tokens.expires_in)
    refresh_expires_at = _expiry(tokens.refresh_expires_in) if tokens.refresh_expires_in else None
    # A requested scope is not a granted scope. Never activate a partial/unknown grant.
    scopes = tokens.scope.split() if tokens.scope else []
    if not (set(base_required_scopes) | set(requested_scopes)).issubset(scopes):
        return _template(
            request, "error.html",
            {"title": "授权范围不完整", "detail": "请同意本次申请的全部权限。原有授权和数据不会修改。"},
            403,
        )
    if send_upgrade and not tokens.refresh_token:
        return _template(
            request, "error.html", {"title": "授权未完成", "detail": "Lark 未返回可续期授权，原有授权保持不变。"}, 502,
        )
    diagnostic_authorization_id = state_row["authorization_id"]
    # Only the local transaction is retried; the Lark code has already been consumed.
    for attempt in range(1, 4):
        stage = "save_lock"
        try:
            with connection() as conn, conn.transaction():
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute("SET LOCAL statement_timeout='15s'")
                lock_sync_material(conn)
                stage = "authorization_lock"
                previous = conn.execute(
                    "SELECT id,status,last_error,body_consent_at FROM authorizations WHERE open_id=%s FOR UPDATE",
                    (profile.open_id,),
                ).fetchone()
                diagnostic_authorization_id = previous["id"] if previous else authorization_id
                if send_upgrade:
                    if (not previous or previous["id"] != state_row["authorization_id"]
                            or previous["status"] not in {"active", "attention_required"}
                            or not previous["body_consent_at"]):
                        return _template(
                            request, "error.html",
                            {"title": "发送授权账号不一致",
                             "detail": "请使用发起增权的本人账号重新授权，原有授权保持不变。"},
                            403,
                        )
                if previous and (send_upgrade or previous["status"] == "active") and conn.execute(
                    """SELECT 1 FROM audit_events WHERE authorization_id=%s
                       AND action IN ('oauth_profile_permissions_upgraded','oauth_send_permissions_upgraded')
                       AND created_at>=%s LIMIT 1""",
                    (previous["id"], state_row["created_at"]),
                ).fetchone():
                    return _template(
                        request, "error.html",
                        {"title": "权限已更新", "detail": "这个旧授权链接已失效，请返回网站首页登录。"}, 409,
                    )
                profile_upgrade = bool(
                    previous and previous["status"] == "attention_required"
                    and previous["last_error"] == PROFILE_SCOPE_UPGRADE_REASON and previous["body_consent_at"]
                )
                preserve_sync = profile_upgrade or send_upgrade
                stage = "authorization_save"
                row = conn.execute(
                    """
                    INSERT INTO authorizations (
                        id, open_id, union_id, display_name, avatar_url,
                        access_token_ciphertext, refresh_token_ciphertext,
                        access_token_expires_at, refresh_token_expires_at, scopes,
                        status, last_error, body_consent_at, p2p_discovery_status,
                        authorized_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', NULL, NOW(), 'pending', NOW(), NOW())
                    ON CONFLICT (open_id) DO UPDATE SET
                        union_id = EXCLUDED.union_id,
                        display_name = EXCLUDED.display_name,
                        avatar_url = EXCLUDED.avatar_url,
                        access_token_ciphertext = EXCLUDED.access_token_ciphertext,
                        refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext,
                        access_token_expires_at = EXCLUDED.access_token_expires_at,
                        refresh_token_expires_at = EXCLUDED.refresh_token_expires_at,
                        scopes = EXCLUDED.scopes,
                        status = 'active',
                        last_error = NULL,
                        body_consent_at = CASE WHEN %s THEN authorizations.body_consent_at ELSE NOW() END,
                        p2p_discovery_status = CASE WHEN %s THEN authorizations.p2p_discovery_status ELSE 'pending' END,
                        p2p_last_checked_at = CASE WHEN %s THEN authorizations.p2p_last_checked_at ELSE NULL END,
                        p2p_last_error = CASE WHEN %s THEN authorizations.p2p_last_error ELSE NULL END,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        authorization_id,
                        profile.open_id,
                        profile.union_id,
                        profile.display_name,
                        safe_avatar_url(profile.avatar_url),
                        cipher.encrypt(tokens.access_token),
                        cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None,
                        token_expires_at,
                        refresh_expires_at,
                        scopes,
                        preserve_sync, preserve_sync, preserve_sync, preserve_sync,
                    ),
                ).fetchone()
                authorization_id = row["id"]
                # Page commits lock sync_jobs before authorization_chats. Preserve
                # that order here so OAuth reauthorization cannot deadlock with a
                # late page response.
                stage = "sync_queue"
                if not send_upgrade:
                    _queue_job(conn, authorization_id, "incremental" if profile_upgrade else "backfill")
                stage = "identity_cache"
                conn.execute(
                    """INSERT INTO lark_identities (open_id, display_name, first_seen_at, last_seen_at)
                       VALUES (%s, %s, NOW(), NOW()) ON CONFLICT (open_id) DO UPDATE
                       SET last_seen_at = EXCLUDED.last_seen_at""", (profile.open_id, profile.name),
                )
                profile_label = update_identity_names(
                    conn, profile.open_id, name=profile.name, en_name=profile.en_name,
                    en_name_present=profile.en_name_present, authoritative=True,
                )
                store_profile_avatar(conn, profile.open_id, profile.avatar_url, avatar_present=profile.avatar_present)
                conn.execute(
                    "UPDATE authorizations SET display_name=%s WHERE id=%s AND display_name IS DISTINCT FROM %s",
                    (profile_label, authorization_id, profile_label),
                )
                stage = "chat_backfill"
                if not preserve_sync:
                    _reset_chat_backfill(conn, authorization_id)
                stage = "profile_provider"
                # A new consent is the opportunity to retry previously denied profile reads.
                conn.execute(
                    """UPDATE identity_profile_providers SET scopes=%s,status='pending',error_code=NULL,retry_at=NOW()
                       WHERE authorization_id=%s""", (scopes, authorization_id),
                )
                _audit(conn, authorization_id, "oauth_send_permissions_upgraded" if send_upgrade else
                       "oauth_profile_permissions_upgraded" if profile_upgrade else "oauth_authorized")
                destination = "/dashboard"
                if send_upgrade:
                    try:
                        creator_access.require_broadcast_access(conn, authorization_id)
                        destination = "/creators/broadcast"
                    except PermissionError:
                        pass

                stage = "session_save"
                session_token = secrets.token_urlsafe(32)
                conn.execute(
                    """
                    INSERT INTO web_sessions (session_hash, authorization_id, expires_at)
                    VALUES (%s, %s, NOW() + (%s * INTERVAL '1 day'))
                    """,
                    (_sha256(session_token), authorization_id, SESSION_DAYS),
                )
                stage = "commit"
        except psycopg.Error as error:
            if error.sqlstate in {"40P01", "40001", "55P03"} and attempt < 3:
                LOGGER.warning(
                    "oauth_database_retry request_id=%s authorization_id=%s stage=%s sqlstate=%s attempt=%s",
                    request_id, diagnostic_authorization_id, stage, error.sqlstate, attempt,
                )
                time.sleep(0.1 * attempt)
                continue
            return _oauth_database_unavailable(
                request, error, request_id, stage=stage,
                authorization_id=diagnostic_authorization_id,
                send_authorization_id=state_row["authorization_id"],
            )
        break
    response = RedirectResponse(url=destination, status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


def _reset_chat_backfill(conn, authorization_id):
    # Ordinary reauthorization remains a fresh content-consent boundary.
    conn.execute(
        """
        UPDATE authorization_chats
        SET backfill_complete = FALSE, backfill_page_token = NULL,
            backfill_window_start_at = NULL, backfill_window_end_at = NULL,
            incremental_page_token = NULL, incremental_job_id = NULL,
            incremental_window_start_at = NULL, incremental_window_end_at = NULL,
            page_claim_id = NULL, page_claim_job_id = NULL, page_claim_mode = NULL,
            page_claimed_at = NULL, page_lease_until = NULL,
            page_retry_at = NULL,
            member_snapshot_claim_id = NULL, member_snapshot_claimed_at = NULL,
            member_snapshot_lease_until = NULL,
            member_snapshot_retry_at = NULL,
            sync_generation = sync_generation + 1,
            sync_status = 'pending', last_error = NULL,
            member_count_status = 'pending', member_count_checked_at = NULL,
            member_snapshot_status = 'pending', member_snapshot_checked_at = NULL
        WHERE authorization_id = %s
        """,
        (authorization_id,),
    )


@app.get("/dashboard")
def dashboard(request: Request):
    authorization = _current_authorization(request)
    if not authorization:
        response = RedirectResponse(url="/", status_code=303)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
    stats = _dashboard_stats(authorization["id"])
    admin_access = admin_permissions.access_for(authorization["id"])
    response = _template(
        request,
        "dashboard.html",
        {
            "authorization": authorization,
            "admin_role": ("owner" if admin_access["is_owner"] else "member"
                           if admin_access["has_admin_access"] else None),
            "admin_landing_path": admin_access["landing_path"],
            "stats": stats,
            "body_consent": bool(authorization["body_consent_at"]),
            "offhours_summary_visible": (
                authorization["status"] == "active" and (
                    bool(authorization.get("body_consent_at"))
                    or admin_access["is_owner"]
                    or _is_remi_tester(authorization, admin_access)
                )
            ),
            "csrf_token": _admin_csrf_token(authorization),
        },
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _is_remi_tester(authorization: dict[str, Any], admin_access: dict[str, Any]) -> bool:
    """Named frontend testers do not need access to any administration page."""
    actor_id = str(authorization.get("id"))
    return bool(
        authorization.get("status") == "active"
        and (
            actor_id == str(REMI_TEST_SHANG_MA_ID)
            or (actor_id == str(REMI_TEST_OWNER_ID) and admin_access.get("is_owner"))
        )
    )


@app.get("/dashboard/stats")
def dashboard_stats(request: Request):
    authorization = _current_authorization(request)
    if not authorization:
        raise HTTPException(status_code=401, detail="sign in required")
    return _template(request, "partials/stats.html", {"stats": _dashboard_stats(authorization["id"])})


def _current_authorization(request: Request) -> dict[str, Any] | None:
    raw_session = request.cookies.get(SESSION_COOKIE)
    if not raw_session:
        return None
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """
            SELECT a.id, a.open_id, a.display_name, a.status, a.last_error, a.scopes,
                   a.body_consent_at, a.p2p_discovery_status, a.p2p_last_error, a.broadcast_enabled,
                   s.last_seen_at < NOW() - INTERVAL '5 minutes' AS touch_session
            FROM web_sessions s
            JOIN authorizations a ON a.id = s.authorization_id
            WHERE s.session_hash = %s AND s.expires_at > NOW()
            """,
            (_sha256(raw_session),),
        ).fetchone()
        if row and row.pop("touch_session", False):
            conn.execute(
                """UPDATE web_sessions SET last_seen_at = NOW() WHERE session_hash = %s
                   AND last_seen_at < NOW() - INTERVAL '5 minutes'""",
                (_sha256(raw_session),),
            )
        return row


def _dashboard_stats(authorization_id: uuid.UUID) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            """
            WITH conversation_counts AS (
                SELECT
                    access.authorization_id,
                    COUNT(*) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') = 'normal'
                    ) AS syncable_conversation_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') = 'normal'
                          AND access.backfill_complete
                    ) AS backfill_complete_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') = 'normal'
                          AND access.sync_status = 'syncing'
                    ) AS syncing_conversation_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') = 'normal'
                          AND access.sync_status IN ('pending', 'failed_retryable')
                    ) AS waiting_conversation_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') = 'normal'
                          AND access.sync_status = 'not_readable'
                    ) AS not_readable_conversation_count,
                    COUNT(*) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') <> 'normal'
                    ) AS archived_conversation_count,
                    MAX(COALESCE(access.last_incremental_at, access.last_backfill_at)) FILTER (
                        WHERE COALESCE(c.chat_status, 'normal') = 'normal'
                    ) AS last_synced_at
                FROM authorization_chats AS access
                JOIN chats AS c ON c.chat_id = access.chat_id
                WHERE access.authorization_id = %s AND access.is_visible
                GROUP BY access.authorization_id
            ), latest_job AS (
                SELECT mode, status, scheduled_at, pages_processed, chats_discovered,
                       messages_indexed, own_messages_indexed, discovery_status,
                       last_http_status, last_error_code, last_error, updated_at
                FROM sync_jobs
                WHERE authorization_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            )
            SELECT
                a.status AS authorization_status,
                COALESCE(c.syncable_conversation_count, 0) AS syncable_conversation_count,
                COALESCE(c.backfill_complete_count, 0) AS backfill_complete_count,
                COALESCE(c.syncing_conversation_count, 0) AS syncing_conversation_count,
                COALESCE(c.waiting_conversation_count, 0) AS waiting_conversation_count,
                COALESCE(c.not_readable_conversation_count, 0) AS not_readable_conversation_count,
                COALESCE(c.archived_conversation_count, 0) AS archived_conversation_count,
                c.last_synced_at,
                j.mode AS job_mode,
                j.status AS job_status,
                j.scheduled_at AS job_scheduled_at,
                j.discovery_status AS job_discovery_status,
                j.last_http_status AS job_last_http_status,
                j.last_error_code AS job_last_error_code,
                j.pages_processed,
                j.chats_discovered,
                j.messages_indexed,
                j.own_messages_indexed,
                j.last_error,
                j.updated_at
            FROM authorizations a
            LEFT JOIN conversation_counts c ON c.authorization_id = a.id
            LEFT JOIN latest_job j ON TRUE
            WHERE a.id = %s
            """,
            (authorization_id, authorization_id, authorization_id),
        ).fetchone()

    facts = dict(row) if row else {"authorization_status": "attention_required"}
    status, badge, detail = sync_summary(facts)
    job = {
        "mode": facts.get("job_mode"),
        "status": facts.get("job_status"),
        "pages_processed": facts.get("pages_processed"),
        "chats_discovered": facts.get("chats_discovered"),
        "messages_indexed": facts.get("messages_indexed"),
        "own_messages_indexed": facts.get("own_messages_indexed"),
        "last_error": facts.get("last_error"),
        "updated_at": facts.get("updated_at"),
    }
    return {
        "last_synced_at": _format_time(facts.get("last_synced_at")),
        "status": status,
        "detail": detail,
        "badge": badge,
        "syncing": job["status"] == "running" and badge == "primary",
        "job": job,
    }


def _queue_job(conn: psycopg.Connection, authorization_id: uuid.UUID, mode: str) -> bool:
    existing = conn.execute(
        """
        UPDATE sync_jobs
        SET mode = %s, status = 'queued', scheduled_at = NOW(), started_at = NULL,
            finished_at = NULL, lease_until = NULL, attempts = 0,
            pages_processed = 0, chats_discovered = 0, messages_indexed = 0,
            own_messages_indexed = 0, discovery_status = 'pending',
            discovery_generation = discovery_generation + 1,
            discovery_completed_at = NULL, last_progress_at = NULL,
            last_error_code = NULL, last_http_status = NULL, last_error = NULL, updated_at = NOW()
        WHERE authorization_id = %s AND status IN ('queued', 'running')
        RETURNING id
        """,
        (mode, authorization_id),
    ).fetchone()
    if existing:
        return True
    row = conn.execute(
        """
        INSERT INTO sync_jobs (id, authorization_id, mode, status, scheduled_at)
        VALUES (%s, %s, %s, 'queued', NOW())
        ON CONFLICT (authorization_id) WHERE status IN ('queued', 'running') DO NOTHING
        RETURNING id
        """,
        (uuid.uuid4(), authorization_id, mode),
    ).fetchone()
    return row is not None


def _audit(conn: psycopg.Connection, authorization_id: uuid.UUID, action: str) -> None:
    conn.execute(
        "INSERT INTO audit_events (id, authorization_id, action) VALUES (%s, %s, %s)",
        (uuid.uuid4(), authorization_id, action),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _expiry(seconds: int | None) -> datetime | None:
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds else None


def _format_time(value: datetime | None) -> str:
    if not value:
        return "尚未完成"
    return value.astimezone(LONDON_TIME_ZONE).strftime("%Y-%m-%d %H:%M %Z")


def _admin_csrf_token(authorization: dict[str, Any]) -> str:
    return hmac.new(
        get_settings().token_encryption_key,
        f"admin-write:{authorization['id']}".encode(),
        hashlib.sha256,
    ).hexdigest()


app.include_router(
    admin.create_admin_router(
        current_authorization=_current_authorization,
        render_template=_template,
        csrf_token=_admin_csrf_token,
    )
)
app.include_router(offhours_summary_routes.create_router(
    current_authorization=_current_authorization,
    render_template=_template, csrf_token=_admin_csrf_token,
))
app.include_router(assistant_routes.create_router(
    current_authorization=_current_authorization, csrf_token=_admin_csrf_token,
))
app.include_router(creator_handles_routes.create_router(
    current_authorization=_current_authorization, render_template=_template, csrf_token=_admin_csrf_token,
))
app.include_router(creator_handle_matching_routes.create_router(
    current_authorization=_current_authorization,render_template=_template,csrf_token=_admin_csrf_token,
))
app.include_router(creator_responsibility_routes.create_router(
    current_authorization=_current_authorization, render_template=_template, csrf_token=_admin_csrf_token,
))
app.include_router(creator_group_routes.create_router(
    current_authorization=_current_authorization, render_template=_template, csrf_token=_admin_csrf_token,
))
app.include_router(desktop_reply_routes.create_router(
    current_authorization=_current_authorization, csrf_token=_admin_csrf_token,
))
app.include_router(desktop_auth_routes.create_router(
    current_authorization=_current_authorization, csrf_token=_admin_csrf_token,
))

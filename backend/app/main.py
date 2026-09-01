"""FastAPI Web app for Lark OAuth and the per-user message statistics dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .admin import create_admin_router
from .admin_reporting import admin_role_for
from .config import get_settings
from .crypto import TokenCipher
from .db import connection, database_is_ready
from .lark_api import LarkApiError, LarkClient

APP_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

SESSION_COOKIE = "__Host-lark_message_stats"
OAUTH_STATE_COOKIE = "__Host-lark_oauth_state"
SESSION_DAYS = 30
LONDON_TIME_ZONE = ZoneInfo("Europe/London")


def _template(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
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


@app.get("/auth/lark/start")
def start_lark_authorization(request: Request):
    settings = get_settings()
    if not settings.app_secret_configured:
        return _template(
            request,
            "error.html",
            {"title": "服务尚未配置", "detail": "管理员尚未在服务器完成 App Secret 的安全录入。"},
            status_code=503,
        )

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _code_challenge(verifier)
    cipher = TokenCipher(settings.token_encryption_key)

    with connection() as conn, conn.transaction():
        conn.execute("DELETE FROM oauth_states WHERE expires_at <= NOW()")
        conn.execute(
            """
            INSERT INTO oauth_states (state_hash, code_verifier_ciphertext, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '10 minutes')
            """,
            (_sha256(state), cipher.encrypt(verifier)),
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
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not code or not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return _template(request, "error.html", {"title": "授权校验失败", "detail": "请返回首页重新开始授权。"}, 400)

    settings = get_settings()
    if not settings.app_secret_configured:
        return _template(request, "error.html", {"title": "服务尚未配置", "detail": "请联系管理员完成安全录入。"}, 503)

    cipher = TokenCipher(settings.token_encryption_key)
    with connection() as conn, conn.transaction():
        state_row = conn.execute(
            """
            DELETE FROM oauth_states
            WHERE state_hash = %s AND expires_at > NOW()
            RETURNING code_verifier_ciphertext
            """,
            (_sha256(state),),
        ).fetchone()
    if not state_row:
        return _template(request, "error.html", {"title": "授权已过期", "detail": "请返回首页重新开始授权。"}, 400)

    try:
        verifier = cipher.decrypt(state_row["code_verifier_ciphertext"])
        lark = LarkClient(settings)
        tokens = lark.exchange_code(code, verifier)
        profile = lark.user_info(tokens.access_token)
    except (LarkApiError, ValueError, RuntimeError):
        return _template(
            request,
            "error.html",
            {"title": "Lark 授权未完成", "detail": "Lark 未返回可用授权。请检查应用可用范围和权限后重试。"},
            502,
        )

    authorization_id = uuid.uuid4()
    token_expires_at = _expiry(tokens.expires_in)
    refresh_expires_at = _expiry(tokens.refresh_expires_in) if tokens.refresh_expires_in else None
    scopes = tokens.scope.split() if tokens.scope else list(settings.oauth_scopes)
    with connection() as conn, conn.transaction():
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
                body_consent_at = NOW(),
                p2p_discovery_status = 'pending',
                p2p_last_checked_at = NULL,
                p2p_last_error = NULL,
                updated_at = NOW()
            RETURNING id
            """,
            (
                authorization_id,
                profile.open_id,
                profile.union_id,
                profile.name,
                profile.avatar_url,
                cipher.encrypt(tokens.access_token),
                cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None,
                token_expires_at,
                refresh_expires_at,
                scopes,
            ),
        ).fetchone()
        authorization_id = row["id"]
        # A re-authorization is the explicit opportunity to retry unreadable
        # conversations and collect content under the revised agreement.
        conn.execute(
            """
            UPDATE authorization_chats
            SET backfill_complete = FALSE, backfill_page_token = NULL,
                backfill_window_start_at = NULL, backfill_window_end_at = NULL,
                incremental_page_token = NULL, incremental_job_id = NULL,
                incremental_window_start_at = NULL, incremental_window_end_at = NULL,
                sync_generation = sync_generation + 1,
                sync_status = CASE WHEN sync_status = 'not_readable' THEN 'pending' ELSE sync_status END,
                last_error = CASE WHEN sync_status = 'not_readable' THEN NULL ELSE last_error END,
                member_count_status = 'pending', member_count_checked_at = NULL
            WHERE authorization_id = %s
            """,
            (authorization_id,),
        )
        _audit(conn, authorization_id, "oauth_authorized")
        _queue_job(conn, authorization_id, "backfill")

        session_token = secrets.token_urlsafe(32)
        conn.execute(
            """
            INSERT INTO web_sessions (session_hash, authorization_id, expires_at)
            VALUES (%s, %s, NOW() + (%s * INTERVAL '1 day'))
            """,
            (_sha256(session_token), authorization_id, SESSION_DAYS),
        )

    response = RedirectResponse(url="/dashboard", status_code=303)
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


@app.get("/dashboard")
def dashboard(request: Request):
    authorization = _current_authorization(request)
    if not authorization:
        return RedirectResponse(url="/", status_code=303)
    stats = _dashboard_stats(authorization["id"])
    return _template(
        request,
        "dashboard.html",
        {
            "authorization": authorization,
            "admin_role": admin_role_for(authorization["id"]),
            "stats": stats,
            "body_consent": bool(authorization["body_consent_at"]),
        },
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
            SELECT a.id, a.open_id, a.display_name, a.status, a.last_error,
                   a.body_consent_at, a.p2p_discovery_status, a.p2p_last_error
            FROM web_sessions s
            JOIN authorizations a ON a.id = s.authorization_id
            WHERE s.session_hash = %s AND s.expires_at > NOW()
            """,
            (_sha256(raw_session),),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE web_sessions SET last_seen_at = NOW() WHERE session_hash = %s",
                (_sha256(raw_session),),
            )
        return row


def _dashboard_stats(authorization_id: uuid.UUID) -> dict[str, Any]:
    with connection() as conn:
        last_sync = conn.execute(
            """
            SELECT
                MAX(COALESCE(access.last_incremental_at, access.last_backfill_at)) AS last_synced_at
            FROM authorization_chats AS access
            JOIN chats AS c ON c.chat_id = access.chat_id
            WHERE access.authorization_id = %s AND access.is_visible
              AND COALESCE(c.chat_status, 'normal') = 'normal'
            """,
            (authorization_id,),
        ).fetchone()
        job = conn.execute(
            """
            SELECT mode, status, pages_processed, chats_discovered, messages_indexed,
                   own_messages_indexed, last_error, updated_at
            FROM sync_jobs WHERE authorization_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (authorization_id,),
        ).fetchone()

    status, detail, badge = _job_status(job)
    return {
        "last_synced_at": _format_time(last_sync["last_synced_at"]),
        "status": status,
        "detail": detail,
        "badge": badge,
        "job": job,
    }


def _job_status(job: dict[str, Any] | None) -> tuple[str, str, str]:
    if not job:
        return "准备接入", "授权完成后，系统会开始建立提醒所需的连接。", "secondary"
    if job["status"] in {"queued", "running"}:
        return "正在接入", "正在建立提醒所需的连接。", "primary"
    if job["status"] == "succeeded":
        return "接入完成", "提醒规则正在测试中；当前不会自动发送消息。", "success"
    if job["status"] == "attention_required":
        return "需要重新授权", "提醒所需的授权需要重新确认。", "warning"
    return "接入遇到问题", "系统会安全重试；若持续失败，请联系管理员。", "danger"


def _queue_job(conn: psycopg.Connection, authorization_id: uuid.UUID, mode: str) -> bool:
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


app.include_router(
    create_admin_router(
        current_authorization=_current_authorization,
        render_template=_template,
    )
)

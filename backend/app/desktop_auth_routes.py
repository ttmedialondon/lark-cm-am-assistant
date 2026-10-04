"""Explicit browser consent bridges an existing web session to the desktop app."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from urllib.parse import parse_qs, urlencode

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .crypto import TokenCipher
from .db import connection
from .desktop_reply_auth import SESSION_COOKIE
from .http_helpers import csrf_matches, form_body

MAX_BODY_BYTES = 4096
_PKCE = re.compile(r"[A-Za-z0-9_-]{43}")
_STATE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_REDIRECT = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/callback")


class DesktopAuthError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def secure(response, redirect=None):
    form_action = "'self'" + (" " + _redirect(redirect) if redirect is not None else "")
    response.headers.update({
        "Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (f"default-src 'none'; form-action {form_action}; "
                                    "frame-ancestors 'none'; base-uri 'none'"),
    })
    return response


def denied(exc):
    return secure(JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status))


def invalid():
    return DesktopAuthError("invalid_desktop_auth_request", "桌面连接请求无效，请从桌面助手重新发起。")


def _redirect(value):
    match = _REDIRECT.fullmatch(value) if isinstance(value, str) else None
    if not match or int(match[1]) > 65535:
        raise invalid()
    return value


def _start_params(value):
    required = {"redirect_uri", "state", "code_challenge"}
    if not required <= value.keys() or value.keys() - required - {"code_challenge_method", "delivery"}:
        raise invalid()
    if value.get("code_challenge_method", "S256") != "S256":
        raise invalid()
    if not isinstance(value["state"], str) or not _STATE.fullmatch(value["state"]):
        raise invalid()
    if not isinstance(value["code_challenge"], str) or not _PKCE.fullmatch(value["code_challenge"]):
        raise invalid()
    delivery = value.get("delivery", "loopback")
    if delivery not in {"loopback", "poll"}:
        raise invalid()
    redirect = value["redirect_uri"]
    if delivery == "poll":
        if redirect != "desktop_poll":
            raise invalid()
    else:
        redirect = _redirect(redirect)
    return {"redirect_uri": redirect, "state": value["state"],
            "code_challenge": value["code_challenge"], "delivery": delivery}


def _single_values(value):
    if any(len(items) != 1 for items in value.values()):
        raise invalid()
    return {key: items[0] for key, items in value.items()}


def _actor(request, current_authorization):
    actor = current_authorization(request)
    if not actor or actor.get("status") != "active" or not request.cookies.get(SESSION_COOKIE):
        raise DesktopAuthError("unauthorized", "当前网页没有有效登录，请使用已登录主系统的浏览器。", 401)
    return actor


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cipher(key):
    return TokenCipher(hmac.digest(key, b"lark-message-stats/desktop-auth-key/v1", "sha256"),
                       b"lark-message-stats/desktop-auth-session/v1")


def _issue(actor, raw_session, params):
    code = secrets.token_urlsafe(32)
    sealed = _cipher(get_settings().token_encryption_key).encrypt(raw_session)
    with connection() as conn, conn.transaction():
        if params["delivery"] == "poll":
            _poll_lock(conn, params["code_challenge"])
        session = conn.execute(
            """SELECT s.expires_at FROM web_sessions s JOIN authorizations a ON a.id=s.authorization_id
               WHERE s.session_hash=%s AND a.id=%s AND a.status='active' AND s.expires_at>clock_timestamp()
               FOR SHARE OF s,a""", (_hash(raw_session), actor["id"]),
        ).fetchone()
        if not session:
            raise DesktopAuthError("unauthorized", "网页登录已失效，请使用有效的现有登录。", 401)
        conn.execute("DELETE FROM desktop_auth_codes WHERE expires_at<=clock_timestamp()")
        if params["delivery"] == "poll":
            conn.execute("DELETE FROM desktop_auth_codes WHERE code_challenge=%s AND redirect_uri='desktop_poll'",
                         (params["code_challenge"],))
        conn.execute(
            """INSERT INTO desktop_auth_codes(code_hash,authorization_id,session_hash,session_ciphertext,
                                              code_challenge,redirect_uri,expires_at)
               VALUES(%s,%s,%s,%s,%s,%s,LEAST(%s,clock_timestamp()+INTERVAL '60 seconds'))""",
            (_hash(code), actor["id"], _hash(raw_session), sealed, params["code_challenge"],
             params["redirect_uri"], session["expires_at"]),
        )
    return code


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def _consume(conn, row):
    failure = DesktopAuthError("invalid_desktop_code", "连接确认已过期或无效，请从桌面助手重新发起。")
    try:
        raw_session = _cipher(get_settings().token_encryption_key).decrypt(row["session_ciphertext"])
    except (InvalidTag, ValueError, UnicodeError):
        raise failure from None
    if not hmac.compare_digest(_hash(raw_session), row["session_hash"]):
        raise failure
    conn.execute("DELETE FROM desktop_auth_codes WHERE code_hash=%s", (row["code_hash"],))
    return {"session_token": raw_session,
            "authorization": {"id": str(row["authorization_id"]), "display_name": row["display_name"]},
            "expires_at": row["session_expires_at"].isoformat()}


def _exchange(value):
    if not isinstance(value, dict) or set(value) != {"code", "code_verifier", "redirect_uri"}:
        raise invalid()
    if any(not isinstance(value[key], str) or not _PKCE.fullmatch(value[key]) for key in ("code", "code_verifier")):
        raise invalid()
    redirect = _redirect(value["redirect_uri"])
    challenge = _challenge(value["code_verifier"])
    failure = DesktopAuthError("invalid_desktop_code", "连接确认已过期或无效，请从桌面助手重新发起。")
    with connection() as conn, conn.transaction():
        row = conn.execute(
            """SELECT c.*,s.expires_at AS session_expires_at,a.display_name FROM desktop_auth_codes c
               JOIN web_sessions s ON s.session_hash=c.session_hash AND s.authorization_id=c.authorization_id
               JOIN authorizations a ON a.id=c.authorization_id
               WHERE c.code_hash=%s AND c.expires_at>clock_timestamp()
                 AND s.expires_at>clock_timestamp() AND a.status='active'
               FOR UPDATE OF c FOR SHARE OF s,a""", (_hash(value["code"]),),
        ).fetchone()
        if not row or row["redirect_uri"] != redirect or not hmac.compare_digest(row["code_challenge"], challenge):
            raise failure
        return _consume(conn, row)


def _poll_lock(conn, challenge):
    # One short transaction per flow also serializes duplicate browser approvals.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("desktop_poll:" + challenge,))


def _poll(value):
    if (not isinstance(value, dict) or set(value) != {"code_verifier", "redirect_uri"}
            or value["redirect_uri"] != "desktop_poll" or not isinstance(value["code_verifier"], str)
            or not _PKCE.fullmatch(value["code_verifier"])):
        raise invalid()
    challenge = _challenge(value["code_verifier"])
    with connection() as conn, conn.transaction():
        _poll_lock(conn, challenge)
        row = conn.execute(
            """SELECT c.*,s.expires_at AS session_expires_at,a.display_name FROM desktop_auth_codes c
               JOIN web_sessions s ON s.session_hash=c.session_hash AND s.authorization_id=c.authorization_id
               JOIN authorizations a ON a.id=c.authorization_id
               WHERE c.code_challenge=%s AND c.redirect_uri='desktop_poll' AND c.expires_at>clock_timestamp()
                 AND s.expires_at>clock_timestamp() AND a.status='active'
               ORDER BY c.created_at DESC LIMIT 1 FOR UPDATE OF c FOR SHARE OF s,a""", (challenge,),
        ).fetchone()
        return _consume(conn, row) if row else None


async def _body(request, content_type):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != content_type:
        raise DesktopAuthError("invalid_content_type", "连接请求的内容类型无效。", 415)
    lengths = request.headers.getlist("content-length")
    if lengths:
        try:
            if len(lengths) != 1 or int(lengths[0]) < 0:
                raise ValueError
            if int(lengths[0]) > MAX_BODY_BYTES:
                raise DesktopAuthError("request_too_large", "连接请求过大。", 413)
        except ValueError:
            raise invalid() from None
    body = await form_body(request)
    if len(body) > MAX_BODY_BYTES:
        raise DesktopAuthError("request_too_large", "连接请求过大。", 413)
    try:
        return body.decode("utf-8")
    except UnicodeError:
        raise invalid() from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _json_body(request):
    body = await _body(request, "application/json")
    try:
        return json.loads(body, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise invalid() from None


def create_router(*, current_authorization, csrf_token):
    router = APIRouter(prefix="/auth/desktop", include_in_schema=False)

    @router.get("/start")
    def start(request: Request):
        try:
            params = _start_params(_single_values({key: request.query_params.getlist(key)
                                                  for key in request.query_params}))
            actor = _actor(request, current_authorization)
            fields = dict(params, csrf_token=csrf_token(actor), confirm="yes")
            hidden = "".join(f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value, quote=True)}">'
                             for key, value in fields.items())
            name = html.escape(actor.get("display_name") or "当前登录账号")
            page = (f'<!doctype html><html lang="zh"><meta charset="utf-8"><title>连接桌面回复助手</title>'
                    f'<h1>连接桌面回复助手</h1><p>当前网页登录账号：<strong>{name}</strong></p>'
                    '<p>确认后桌面助手将使用此账号生成回复，不会重新授权 Lark。不是您的账号时，请关闭此页。</p>'
                    f'<form method="post" action="/auth/desktop/start">{hidden}'
                    f'<button type="submit">确认使用 {name} 连接桌面助手</button></form></html>')
            return secure(HTMLResponse(page), params["redirect_uri"] if params["delivery"] == "loopback" else None)
        except DesktopAuthError as exc:
            return denied(exc)

    @router.post("/start")
    async def approve(request: Request):
        try:
            actor = await run_in_threadpool(_actor, request, current_authorization)
            body = await _body(request, "application/x-www-form-urlencoded")
            try:
                form = _single_values(parse_qs(body, keep_blank_values=True, strict_parsing=True, max_num_fields=7))
            except ValueError:
                raise invalid() from None
            if not csrf_matches(form, csrf_token(actor)):
                raise DesktopAuthError("csrf_invalid", "请求校验失败，请从桌面助手重新发起。", 403)
            if form.pop("confirm", None) != "yes":
                raise invalid()
            form.pop("csrf_token", None)
            params = _start_params(form)
            code = await run_in_threadpool(_issue, actor, request.cookies[SESSION_COOKIE], params)
            if params["delivery"] == "poll":
                return secure(HTMLResponse('<!doctype html><html lang="zh"><meta charset="utf-8">'
                                           '<title>身份已确认</title><h1>身份已确认，请返回桌面助手</h1></html>'))
            callback = params["redirect_uri"] + "?" + urlencode({"code": code, "state": params["state"]})
            return secure(RedirectResponse(callback, status_code=303), params["redirect_uri"])
        except DesktopAuthError as exc:
            return denied(exc)

    @router.post("/exchange")
    async def exchange(request: Request):
        try:
            result = await run_in_threadpool(_exchange, await _json_body(request))
            return secure(JSONResponse(result))
        except DesktopAuthError as exc:
            return denied(exc)

    @router.post("/poll")
    async def poll(request: Request):
        try:
            result = await run_in_threadpool(_poll, await _json_body(request))
            return secure(JSONResponse(result if result is not None else {"status": "pending"},
                                       status_code=200 if result is not None else 202))
        except DesktopAuthError as exc:
            return denied(exc)

    return router

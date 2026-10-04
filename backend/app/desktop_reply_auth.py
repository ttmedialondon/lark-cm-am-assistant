"""Desktop replies reuse existing web sessions without issuing another credential."""

import re

from starlette.requests import Request

from .http_helpers import csrf_matches

SESSION_COOKIE = "__Host-lark_message_stats"


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def authenticate(request: Request, current_authorization, csrf_token) -> dict:
    """A supplied Bearer takes priority; cookie writes retain the existing CSRF check."""
    authorization_headers = request.headers.getlist("authorization")
    if authorization_headers:
        match = re.fullmatch(r"(?i:Bearer) +([A-Za-z0-9._~+/-]+=*)", authorization_headers[0])
        if len(authorization_headers) != 1 or not match:
            raise AuthError("unauthorized", "登录已失效，请重新登录。", 401)
        # Delegate to the same hash/expiry resolver; only this supplied session can authenticate.
        scope = dict(request.scope)
        scope["headers"] = [
            (name, value) for name, value in scope.get("headers", [])
            if name.lower() not in {b"authorization", b"cookie"}
        ] + [(b"cookie", f"{SESSION_COOKIE}={match[1]}".encode("ascii"))]
        authorization = current_authorization(Request(scope))
    else:
        authorization = current_authorization(request)
    if not authorization or authorization.get("status") != "active":
        raise AuthError("unauthorized", "登录已失效，请重新登录。", 401)
    if not authorization_headers and request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = csrf_token(authorization)
        if not expected or not csrf_matches({"csrf_token": request.headers.get("X-CSRF-Token", "")}, expected):
            raise AuthError("csrf_invalid", "请求校验失败，请刷新页面。", 403)
    return authorization

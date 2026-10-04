from contextlib import nullcontext
from uuid import uuid4

import pytest
from starlette.requests import Request

from app import desktop_reply_auth, main


def request(headers=(), method="POST"):
    return Request({"type": "http", "method": method, "path": "/api/desktop/replies",
                    "headers": [(name.lower().encode(), value.encode()) for name, value in headers]})


def test_bearer_uses_only_the_supplied_session_and_rejects_ambiguous_headers():
    actor = {"id": uuid4(), "status": "active"}
    resolved = []

    def resolver(req):
        resolved.append(req)
        return actor

    original = request([
        ("Cookie", f"{main.SESSION_COOKIE}=browser-session; another=cookie"),
        ("Authorization", "bEaReR desktop-session"),
    ])
    assert desktop_reply_auth.authenticate(original, resolver, lambda _: "") is actor
    assert resolved[0].cookies == {main.SESSION_COOKIE: "desktop-session"}
    assert not resolved[0].headers.getlist("authorization")
    assert original.cookies[main.SESSION_COOKIE] == "browser-session"
    for headers in (
        [("Authorization", "")],
        [("Authorization", "Basic desktop-session")],
        [("Authorization", "Bearer")],
        [("Authorization", "Bearer desktop-session; another=cookie")],
        [("Authorization", "Bearer desktop-session"), ("Authorization", "Bearer desktop-session")],
    ):
        with pytest.raises(desktop_reply_auth.AuthError) as failure:
            desktop_reply_auth.authenticate(request(headers), resolver, lambda _: "csrf")
        assert (failure.value.code, failure.value.status) == ("unauthorized", 401)
    assert len(resolved) == 1


def test_cookie_authentication_keeps_existing_write_csrf_and_active_status_checks():
    actor = {"id": uuid4(), "status": "active"}

    def resolver(_):
        return actor

    def csrf(_):
        return "existing-csrf-token"

    assert desktop_reply_auth.authenticate(request(method="GET"), resolver, csrf) is actor
    assert desktop_reply_auth.authenticate(
        request([("X-CSRF-Token", "existing-csrf-token")]), resolver, csrf,
    ) is actor
    for supplied, expected in (("", "existing-csrf-token"), ("wrong", "existing-csrf-token"), ("present", "")):
        with pytest.raises(desktop_reply_auth.AuthError) as failure:
            desktop_reply_auth.authenticate(
                request([("X-CSRF-Token", supplied)]), resolver, lambda _, expected=expected: expected,
            )
        assert (failure.value.code, failure.value.status) == ("csrf_invalid", 403)
    for invalid_actor in (None, {"id": uuid4(), "status": "revoked"}):
        with pytest.raises(desktop_reply_auth.AuthError) as failure:
            desktop_reply_auth.authenticate(
                request(method="GET"), lambda _, invalid_actor=invalid_actor: invalid_actor, csrf,
            )
        assert failure.value.status == 401


def test_bearer_validates_real_session_hash_expiry_and_revocation(pg, monkeypatch):
    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    actor, cookie_actor = uuid4(), uuid4()
    for actor_id, open_id in ((actor, "desktop_actor"), (cookie_actor, "cookie_actor")):
        pg.execute(
            "INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext) VALUES(%s,%s,%s,%s)",
            (actor_id, open_id, "Same CM name", "fixture-lark-token"),
        )
    for token, actor_id, lifetime in (
        ("valid-desktop-session", actor, 60), ("browser-session", cookie_actor, 60),
        ("expired-desktop-session", actor, -60),
    ):
        pg.execute(
            "INSERT INTO web_sessions(session_hash,authorization_id,expires_at) "
            "VALUES(%s,%s,NOW()+(%s*INTERVAL '1 minute'))",
            (main._sha256(token), actor_id, lifetime),
        )

    def authenticate(token):
        return desktop_reply_auth.authenticate(
            request([("Authorization", f"Bearer {token}"),
                     ("Cookie", f"{main.SESSION_COOKIE}=browser-session")]),
            main._current_authorization, lambda _: "",
        )

    assert authenticate("valid-desktop-session")["id"] == actor
    for token in ("expired-desktop-session", "fake-session", "fixture-lark-token", "sk-fixture-api-key"):
        with pytest.raises(desktop_reply_auth.AuthError) as failure:
            authenticate(token)
        assert (failure.value.code, failure.value.status) == ("unauthorized", 401)
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor,))
    with pytest.raises(desktop_reply_auth.AuthError) as failure:
        authenticate("valid-desktop-session")
    assert (failure.value.code, failure.value.status) == ("unauthorized", 401)

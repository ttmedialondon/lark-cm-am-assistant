import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import desktop_auth_routes as routes
from app import main

START = "/auth/desktop/start"
EXCHANGE = "/auth/desktop/exchange"
POLL = "/auth/desktop/poll"


@pytest.fixture
def auth_api(pg, monkeypatch):
    schema = pg.execute("SELECT current_schema() AS name").fetchone()["name"]
    settings = SimpleNamespace(token_encryption_key=b"b" * 32)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    actor = uuid4()
    raw_session = "fixture-web-session"
    pg.execute("INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext) VALUES(%s,%s,%s,'fixture')",
               (actor, str(actor), "Current <CM>"))
    pg.execute("INSERT INTO web_sessions(session_hash,authorization_id,expires_at) "
               "VALUES(%s,%s,NOW()+INTERVAL '1 day')",
               (main._sha256(raw_session), actor))
    # Committed isolated schema plus fresh connections exercise real competing transactions.
    pg.commit()
    pg.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ["CREATOR_WORKFLOW_AI_TEST_DATABASE_URL"]

    @contextmanager
    def fresh_connection():
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
            yield conn

    monkeypatch.setattr(main, "connection", fresh_connection)
    monkeypatch.setattr(routes, "connection", fresh_connection)
    app = FastAPI()
    app.include_router(routes.create_router(current_authorization=main._current_authorization,
                                           csrf_token=main._admin_csrf_token))
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"redirect_uri": "http://127.0.0.1:54321/callback", "state": "s" * 43, "code_challenge": challenge}
    try:
        yield SimpleNamespace(pg=pg, app=app, client=TestClient(app, base_url="https://larkbot.ttapp.help"),
                              actor=actor, session=raw_session, params=params, verifier=verifier,
                              csrf=main._admin_csrf_token({"id": actor}),
                              headers={"Cookie": f"{main.SESSION_COOKIE}={raw_session}"})
    finally:
        pg.rollback()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def approve(api):
    result = api.client.post(START, data=dict(api.params, csrf_token=api.csrf, confirm="yes"),
                             headers=api.headers, follow_redirects=False)
    assert result.status_code == 303
    callback = urlsplit(result.headers["location"])
    fields = parse_qs(callback.query)
    assert set(fields) == {"code", "state"} and fields["state"] == [api.params["state"]]
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == api.params["redirect_uri"]
    return {"code": fields["code"][0], "code_verifier": api.verifier, "redirect_uri": api.params["redirect_uri"]}


def test_confirmed_existing_identity_pkce_and_single_use_session_transfer(auth_api):
    api = auth_api
    page = api.client.get(START, params=api.params, headers=api.headers)
    assert page.status_code == 200 and "Current &lt;CM&gt;" in page.text
    assert "确认使用" in page.text and api.session not in page.text
    for header, value in (("cache-control", "no-store, max-age=0"), ("pragma", "no-cache"),
                          ("referrer-policy", "no-referrer")):
        assert page.headers[header] == value
    assert f"form-action 'self' {api.params['redirect_uri']}" in page.headers["content-security-policy"]
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0
    exchange = approve(api)
    stored = api.pg.execute("SELECT * FROM desktop_auth_codes").fetchone()
    assert stored["code_hash"] == main._sha256(exchange["code"]) and exchange["code"] not in str(stored)
    assert api.session not in stored["session_ciphertext"]
    assert stored["authorization_id"] == api.actor and stored["session_hash"] == main._sha256(api.session)
    assert 0 < (stored["expires_at"] - stored["created_at"]).total_seconds() <= 60
    result = api.client.post(EXCHANGE, json=exchange)
    assert result.status_code == 200
    assert result.json() == {"session_token": api.session,
                             "authorization": {"id": str(api.actor), "display_name": "Current <CM>"},
                             "expires_at": api.pg.execute("SELECT expires_at FROM web_sessions").fetchone()[
                                 "expires_at"].isoformat()}
    assert result.headers["cache-control"] == "no-store, max-age=0"
    assert api.client.post(EXCHANGE, json=exchange).status_code == 400
    assert api.pg.execute("SELECT count(*) AS n FROM web_sessions").fetchone()["n"] == 1
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0


def test_loopback_pkce_and_state_reject_untrusted_start_parameters(auth_api):
    api = auth_api
    invalid = [
        {"redirect_uri": "http://localhost:54321/callback"},
        {"redirect_uri": "http://127.0.0.1:54321/callback?secret=1"},
        {"redirect_uri": "http://user@127.0.0.1:54321/callback"},
        {"redirect_uri": "http://127.0.0.1:54321/callback#fragment"},
        {"redirect_uri": "http://127.0.0.1:65536/callback"},
        {"redirect_uri": "http://127.0.0.1:54321/%63allback"},
        {"code_challenge_method": "plain"}, {"code_challenge": "x" * 42}, {"state": "short"},
        {"delivery": "unknown"}, {"delivery": "poll"}, {"redirect_uri": "desktop_poll"},
    ]
    for change in invalid:
        result = api.client.get(START, params=dict(api.params, **change), headers=api.headers)
        assert result.status_code == 400 and "location" not in result.headers
    duplicates = list(api.params.items()) + [("state", api.params["state"])]
    assert api.client.get(START, params=duplicates, headers=api.headers).status_code == 400
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0


def test_web_login_csrf_and_explicit_account_confirmation_are_required(auth_api):
    api = auth_api
    missing = api.client.get(START, params=api.params)
    assert missing.status_code == 401 and "location" not in missing.headers
    bearer_only = api.client.get(START, params=api.params, headers={"Authorization": f"Bearer {api.session}"})
    assert bearer_only.status_code == 401
    wrong_csrf = dict(api.params, csrf_token="wrong", confirm="yes")
    denied = api.client.post(START, data=wrong_csrf, headers=api.headers)
    assert denied.status_code == 403
    denied = api.client.post(START, data=dict(api.params, csrf_token=api.csrf), headers=api.headers)
    assert denied.status_code == 400
    api.pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (api.actor,))
    api.pg.commit()
    assert api.client.get(START, params=api.params, headers=api.headers).status_code == 401
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0


def test_wrong_pkce_or_redirect_preserves_the_legitimate_grant(auth_api):
    api = auth_api
    exchange = approve(api)
    for changed in ({"code_verifier": "w" * 43}, {"redirect_uri": "http://127.0.0.1:54322/callback"}):
        denied = api.client.post(EXCHANGE, json=dict(exchange, **changed))
        assert denied.status_code == 400
        assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 1
    assert api.client.post(EXCHANGE, json=exchange).status_code == 200


@pytest.mark.parametrize("boundary", ["code_expiry", "session_expiry", "revocation"])
def test_expired_or_revoked_source_cannot_be_transferred(auth_api, boundary):
    api = auth_api
    exchange = approve(api)
    if boundary == "code_expiry":
        api.pg.execute("UPDATE desktop_auth_codes SET expires_at=clock_timestamp()-INTERVAL '1 second'")
    elif boundary == "session_expiry":
        api.pg.execute("UPDATE web_sessions SET expires_at=clock_timestamp()-INTERVAL '1 second'")
    else:
        api.pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (api.actor,))
    api.pg.commit()
    assert api.client.post(EXCHANGE, json=exchange).status_code == 400


def test_competing_exchanges_only_one_receives_the_existing_session(auth_api):
    api = auth_api
    exchange = approve(api)

    def redeem(_):
        with TestClient(api.app, base_url="https://larkbot.ttapp.help") as client:
            return client.post(EXCHANGE, json=exchange)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, range(2)))
    assert sorted(result.status_code for result in results) == [200, 400]
    assert next(result for result in results if result.status_code == 200).json()["session_token"] == api.session
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0


def test_exchange_body_is_bounded_strict_and_never_consumes_on_bad_input(auth_api):
    api = auth_api
    exchange = approve(api)
    assert api.client.post(EXCHANGE, json=dict(exchange, cm_id=str(uuid4()))).status_code == 400
    duplicate = json.dumps(exchange)[:-1] + ',"code":"' + exchange["code"] + '"}'
    assert api.client.post(EXCHANGE, content=duplicate, headers={"Content-Type": "application/json"}).status_code == 400
    assert api.client.post(EXCHANGE, content=b"x" * (routes.MAX_BODY_BYTES + 1),
                           headers={"Content-Type": "application/json"}).status_code == 413
    assert api.client.post(EXCHANGE, data=exchange).status_code == 415
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 1
    assert api.client.post(EXCHANGE, json=exchange).status_code == 200


def test_poll_approval_pkce_pending_strict_body_and_single_delivery(auth_api):
    api = auth_api
    params = dict(api.params, delivery="poll", redirect_uri="desktop_poll")
    body = {"code_verifier": api.verifier, "redirect_uri": "desktop_poll"}
    pending = api.client.post(POLL, json=body)
    assert pending.status_code == 202 and pending.json() == {"status": "pending"}
    assert pending.headers["cache-control"] == "no-store, max-age=0"
    page = api.client.get(START, params=params, headers=api.headers)
    assert page.status_code == 200 and "Current &lt;CM&gt;" in page.text
    assert "http://127.0.0.1" not in page.headers["content-security-policy"]
    form = dict(params, csrf_token=api.csrf, confirm="yes")
    assert api.client.post(START, data=dict(form, csrf_token="wrong"), headers=api.headers).status_code == 403
    for _ in range(2):
        approved = api.client.post(START, data=form, headers=api.headers, follow_redirects=False)
        assert approved.status_code == 200 and "location" not in approved.headers
        assert "身份已确认，请返回桌面助手" in approved.text and api.session not in approved.text
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 1
    wrong = api.client.post(POLL, json=dict(body, code_verifier="w" * 43))
    assert wrong.status_code == 202 and wrong.json() == {"status": "pending"}
    assert api.client.post(POLL, json=dict(body, state=params["state"])).status_code == 400
    assert api.client.post(POLL, json=dict(body, redirect_uri=api.params["redirect_uri"])).status_code == 400
    delivered = api.client.post(POLL, json=body)
    assert delivered.status_code == 200 and delivered.json()["session_token"] == api.session
    assert delivered.json()["authorization"] == {"id": str(api.actor), "display_name": "Current <CM>"}
    assert api.client.post(POLL, json=body).status_code == 202
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0
    assert api.pg.execute("SELECT count(*) AS n FROM web_sessions").fetchone()["n"] == 1
    assert api.client.post(START, data=form, headers=api.headers).status_code == 200
    api.pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (api.actor,))
    api.pg.commit()
    assert api.client.post(POLL, json=body).json() == {"status": "pending"}


def test_concurrent_poll_approvals_keep_one_grant_and_only_one_poll_consumes(auth_api):
    api = auth_api
    form = dict(api.params, delivery="poll", redirect_uri="desktop_poll", csrf_token=api.csrf, confirm="yes")
    body = {"code_verifier": api.verifier, "redirect_uri": "desktop_poll"}

    def approve_poll(_):
        with TestClient(api.app, base_url="https://larkbot.ttapp.help") as client:
            return client.post(START, data=form, headers=api.headers)

    def receive_poll(_):
        with TestClient(api.app, base_url="https://larkbot.ttapp.help") as client:
            return client.post(POLL, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        approvals = list(pool.map(approve_poll, range(2)))
    assert [result.status_code for result in approvals] == [200, 200]
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(receive_poll, range(2)))
    assert sorted(result.status_code for result in results) == [200, 202]
    assert next(result for result in results if result.status_code == 200).json()["session_token"] == api.session
    assert api.pg.execute("SELECT count(*) AS n FROM desktop_auth_codes").fetchone()["n"] == 0

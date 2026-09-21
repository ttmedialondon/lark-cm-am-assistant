from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_identity_names import pg  # noqa: F401

from app import main
from app.crypto import TokenCipher
from app.lark_api import LarkProfile


def _request_with_session(session: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if session:
        headers.append((b"cookie", f"{main.SESSION_COOKIE}={session}".encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_index_starts_lark_oauth_without_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_current_authorization", lambda _: None)
    response = main.index(_request_with_session())
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/lark/start"


def test_index_keeps_authenticated_users_on_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_current_authorization", lambda _: {"id": "authorized"})
    response = main.index(_request_with_session("active-session"))
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_session_touch_is_throttled_without_caching_revocation(pg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    actor = uuid4()
    session_hash = main._sha256("fixture-session")
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'touch_actor','fixture')",
               (actor,))
    pg.execute("INSERT INTO web_sessions(session_hash,authorization_id,expires_at,last_seen_at) "
               "VALUES(%s,%s,NOW()+INTERVAL '1 day',NOW()-INTERVAL '10 minutes')", (session_hash, actor))
    request = _request_with_session("fixture-session")
    assert main._current_authorization(request)["status"] == "active"
    touched = pg.execute("SELECT last_seen_at FROM web_sessions WHERE session_hash=%s", (session_hash,)).fetchone()
    pg.execute("UPDATE web_sessions SET last_seen_at=NOW()-INTERVAL '1 minute' WHERE session_hash=%s", (session_hash,))
    for _ in range(3):
        assert main._current_authorization(request)["id"] == actor
    unchanged = pg.execute("SELECT last_seen_at FROM web_sessions WHERE session_hash=%s", (session_hash,)).fetchone()
    assert touched["last_seen_at"] - unchanged["last_seen_at"] == timedelta(minutes=1)
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor,))
    assert main._current_authorization(request)["status"] == "revoked"
    pg.execute("DELETE FROM web_sessions WHERE session_hash=%s", (session_hash,))
    assert main._current_authorization(request) is None


def test_dashboard_keeps_messages_and_removes_retired_creator_controls() -> None:
    rendered = main.templates.get_template("dashboard.html").render(
        authorization={"display_name": "CM", "p2p_discovery_status": "succeeded"},
        admin_role=None,
        body_consent=True,
        stats={
            "badge": "success", "status": "接入完成", "detail": "当前不会自动发送消息。",
            "last_synced_at": "2026-09-01 12:00 BST", "job": {"status": "succeeded"},
        },
    )
    assert "消息提醒中心" in rendered and "权限与数据使用" in rendered
    for retired in ("/dashboard/creators", "creator-setup-preview", "creator-candidates",
                    "待我确认的达人候选", "历史联系人归属"):
        assert retired not in rendered


def test_dashboard_requires_login_and_retired_creator_endpoints_are_absent(monkeypatch) -> None:
    monkeypatch.setattr(main, "_current_authorization", lambda _: None)
    client = TestClient(main.app)
    response = client.get("/dashboard", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert client.get("/dashboard/creator-setup-preview").status_code == 404
    assert client.post(f"/dashboard/creator-candidates/{uuid4()}/confirm").status_code == 404
    for path in ("/dashboard/creators", "/dashboard/creators/data", "/dashboard/creators/users"):
        assert client.get(path).status_code == 404
    for path in ("/dashboard/creators/ai/scan", "/dashboard/creators/import/preview",
                 f"/dashboard/creators/{uuid4()}/confirm"):
        assert client.post(path).status_code == 404


def test_named_frontend_testers_do_not_require_a_retired_admin_grant() -> None:
    for actor_id, active, owner, expected in (
        (main.REMI_TEST_SHANG_MA_ID, True, False, True),
        (main.REMI_TEST_SHANG_MA_ID, False, False, False),
        (main.REMI_TEST_OWNER_ID, True, True, True),
        (main.REMI_TEST_OWNER_ID, True, False, False),
        (uuid4(), True, False, False),
    ):
        assert main._is_remi_tester(
            {"id": actor_id, "status": "active" if active else "revoked"}, {"is_owner": owner}
        ) is expected


def test_reauthorization_restarts_an_active_job_with_a_new_generation() -> None:
    calls = []

    class Result:
        @staticmethod
        def fetchone():
            return {"id": "active-job"}

    class Connection:
        @staticmethod
        def execute(query, params=None):
            calls.append((query, params))
            return Result()

    assert main._queue_job(Connection(), "authorization-id", "backfill")
    query, params = calls[0]
    assert params == ("backfill", "authorization-id")
    assert "mode = %s, status = 'queued'" in query
    assert "discovery_status = 'pending'" in query
    assert "discovery_generation = discovery_generation + 1" in query
    assert "last_http_status = NULL" in query


def test_profile_scope_upgrade_preserves_history_and_rejects_partial_or_overlapping_grants(pg, monkeypatch):  # noqa: F811
    scopes = (
        "offline_access", "im:chat:read", "im:message:readonly", "im:chat.members:read",
        "contact:contact.base:readonly", "contact:user.base:readonly",
    )
    settings = SimpleNamespace(app_secret_configured=True, token_encryption_key=b"o" * 32, oauth_scopes=scopes)
    cipher = TokenCipher(settings.token_encryption_key)
    actor, person, job = uuid4(), uuid4(), uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Old Profile')", (person,))
    pg.execute(
        "INSERT INTO lark_identities(open_id,contact_id,name,display_name) "
        "VALUES('ou_upgrade',%s,'Old Profile','Old Profile')",
        (person,),
    )
    pg.execute("INSERT INTO lark_identities(open_id) VALUES('ou_counterpart')")
    pg.execute(
        """INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,scopes,status,last_error,
        body_consent_at,p2p_discovery_status,p2p_last_checked_at,p2p_last_error)
        VALUES(%s,'ou_upgrade','Old Profile',%s,%s,'attention_required',%s,NOW()-INTERVAL '30 days',
        'succeeded',NOW()-INTERVAL '1 day','retained p2p detail')""",
        (actor, cipher.encrypt("old-access"), list(scopes[:4]), main.PROFILE_SCOPE_UPGRADE_REASON),
    )
    pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (actor,))
    pg.execute(
        "INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_generation) "
        "VALUES(%s,%s,'backfill','running',9)",
        (job, actor),
    )
    for chat, mode in (("oauth_group", "group"), ("oauth_private", "p2p")):
        pg.execute("INSERT INTO chats(chat_id,chat_mode) VALUES(%s,%s)", (chat, mode))
        pg.execute(
            """INSERT INTO authorization_chats(authorization_id,chat_id,sync_generation,sync_status,
            backfill_complete,backfill_page_token,backfill_window_start_at,backfill_window_end_at,
            incremental_page_token,incremental_job_id,incremental_window_start_at,incremental_window_end_at,
            page_claim_id,page_claim_job_id,page_claim_mode,page_claimed_at,page_lease_until,
            member_snapshot_status,member_snapshot_checked_at,p2p_counterpart_open_id,p2p_counterpart_type)
            VALUES(%s,%s,17,'succeeded',TRUE,'backfill-cursor',NOW()-INTERVAL '7 days',NOW(),
            'incremental-cursor',%s,NOW()-INTERVAL '1 day',NOW(),%s,%s,'incremental',NOW(),
            NOW()+INTERVAL '10 minutes','succeeded',NOW(),%s,%s)""",
            (actor, chat, job, uuid4(), job, "ou_counterpart" if mode == "p2p" else None,
             "user" if mode == "p2p" else None),
        )
    pg.execute(
        "INSERT INTO chat_members(chat_id,open_id,display_name) VALUES('oauth_group','ou_upgrade','Old Profile')"
    )
    pg.execute(
        """INSERT INTO identity_profile_providers(authorization_id,scopes,status,error_code,retry_at)
        VALUES(%s,%s,'unavailable','profile_scope_unavailable',NOW()+INTERVAL '1 day')""",
        (actor, list(scopes[:4])),
    )
    profile = LarkProfile(
        open_id="ou_upgrade", union_id="on_upgrade", name="张三", en_name="Alex",
        avatar_url="https://test.feishucdn.com/avatar.png", en_name_present=True, avatar_present=True,
    )

    class FakeLark:
        def __init__(self, received):
            assert received is settings

        def exchange_code(self, code, verifier):
            assert verifier == "synthetic-verifier"
            granted = None if code == "missing" else " ".join(scopes[:4] if code == "partial" else scopes)
            return SimpleNamespace(
                access_token="new-access-" + code, refresh_token="new-refresh", expires_in=3600,
                refresh_expires_in=86400, scope=granted,
            )

        def user_info(self, token):
            assert token.startswith("new-access-")
            return profile

    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "LarkClient", FakeLark)
    client = TestClient(main.app, base_url="https://testserver")

    def state(*, fresh=False):
        value = uuid4().hex
        # The fixture holds one transaction/NOW(); model separate request timestamps explicitly.
        pg.execute(
            """INSERT INTO oauth_states(state_hash,code_verifier_ciphertext,expires_at,created_at)
            VALUES(%s,%s,NOW()+INTERVAL '10 minutes',NOW()+(%s*INTERVAL '1 second'))""",
            (main._sha256(value), cipher.encrypt("synthetic-verifier"), 1 if fresh else -60),
        )
        return value

    def callback(value, code="full"):
        client.cookies.clear()
        response = client.get(
            "/auth/lark/callback", params={"state": value, "code": code},
            headers={"cookie": f"{main.OAUTH_STATE_COOKIE}={value}"}, follow_redirects=False,
        )
        assert not pg.execute("SELECT 1 FROM oauth_states WHERE state_hash=%s", (main._sha256(value),)).fetchone()
        return response

    def snapshot():
        return {
            table: pg.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("authorizations", "authorization_chats", "admin_roles", "contacts", "lark_identities",
                          "chat_members", "identity_profile_providers", "sync_jobs", "web_sessions", "audit_events")
        }

    original = snapshot()
    overlap = state()
    for grant in ("missing", "partial"):
        assert callback(state(), grant).status_code == 403
        assert snapshot() == original

    response = callback(state())
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"
    upgraded = snapshot()
    auth = upgraded["authorizations"][0]
    assert auth["id"] == actor and auth["open_id"] == "ou_upgrade" and auth["status"] == "active"
    assert auth["last_error"] is None and auth["scopes"] == list(scopes)
    assert cipher.decrypt(auth["access_token_ciphertext"]) == "new-access-full"
    for field in ("body_consent_at", "p2p_discovery_status", "p2p_last_checked_at", "p2p_last_error"):
        assert auth[field] == original["authorizations"][0][field]
    assert upgraded["authorization_chats"] == original["authorization_chats"]
    assert upgraded["admin_roles"] == original["admin_roles"]
    identity = next(row for row in upgraded["lark_identities"] if row["open_id"] == "ou_upgrade")
    assert (identity["name"], identity["en_name"], identity["display_name"], identity["avatar_url"]) == (
        "张三", "Alex", "Alex（张三）", profile.avatar_url,
    )
    assert upgraded["contacts"][0]["id"] == person and upgraded["contacts"][0]["display_name"] == "Alex（张三）"
    assert upgraded["chat_members"][0]["display_name"] == "Alex（张三）"
    provider = upgraded["identity_profile_providers"][0]
    assert provider["scopes"] == list(scopes) and provider["status"] == "pending" and provider["error_code"] is None
    assert upgraded["sync_jobs"][0]["id"] == job and upgraded["sync_jobs"][0]["mode"] == "incremental"
    session = response.cookies[main.SESSION_COOKIE]
    assert upgraded["web_sessions"][0]["session_hash"] == main._sha256(session)
    assert upgraded["web_sessions"][0]["authorization_id"] == actor
    assert [row["action"] for row in upgraded["audit_events"] if row["authorization_id"] == actor] == [
        "oauth_profile_permissions_upgraded"
    ]

    assert callback(overlap).status_code == 409
    assert snapshot() == upgraded  # An older tab cannot reset cursors or replace tokens/profile again.

    assert callback(state(fresh=True), "ordinary").status_code == 303
    ordinary = snapshot()
    assert ordinary["authorizations"][0]["id"] == actor and ordinary["admin_roles"] == original["admin_roles"]
    assert ordinary["authorizations"][0]["body_consent_at"] > auth["body_consent_at"]
    assert ordinary["authorizations"][0]["p2p_discovery_status"] == "pending"
    assert ordinary["authorizations"][0]["p2p_last_checked_at"] is None
    assert ordinary["authorizations"][0]["p2p_last_error"] is None
    for current, previous in zip(ordinary["authorization_chats"], original["authorization_chats"], strict=True):
        assert current["sync_generation"] == previous["sync_generation"] + 1 and not current["backfill_complete"]
        assert current["sync_status"] == current["member_snapshot_status"] == "pending"
        for field in ("backfill_page_token", "incremental_page_token", "backfill_window_start_at",
                      "backfill_window_end_at", "incremental_job_id", "incremental_window_start_at",
                      "incremental_window_end_at", "page_claim_id", "page_claim_job_id", "page_claim_mode",
                      "page_claimed_at", "page_lease_until", "member_snapshot_checked_at"):
            assert current[field] is None
    assert ordinary["sync_jobs"][0]["mode"] == "backfill" and len(ordinary["web_sessions"]) == 2

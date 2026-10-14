from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import creator_broadcast, main
from app.config import Settings
from app.crypto import TokenCipher
from app.lark_api import LarkApiError, LarkProfile


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


def test_oauth_database_unavailable_is_recoverable_without_leaking_or_replacing_login(monkeypatch, caplog):
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(
        app_secret_configured=True, token_encryption_key=b"o" * 32, oauth_scopes=("offline_access",),
    ))

    def unavailable():
        raise psycopg.OperationalError("private SQL, token=fixture-token, body=fixture-body")

    monkeypatch.setattr(main, "connection", unavailable)
    client = TestClient(main.app, base_url="https://testserver")
    for path in ("/auth/lark/start", "/auth/lark/start?send=1",
                 "/auth/lark/callback?code=fixture-code&state=fixture-state"):
        response = client.get(path, headers={"cookie": f"{main.OAUTH_STATE_COOKIE}=fixture-state; "
                                            f"{main.SESSION_COOKIE}=existing-login"}, follow_redirects=False)
        assert response.status_code == 503 and "重新授权" in response.text
        assert response.headers["x-request-id"] in response.text
        assert response.headers["cache-control"] == "no-store"
        assert main.SESSION_COOKIE not in response.cookies
    assert "oauth_database_failed" in caplog.text
    assert all(secret not in caplog.text for secret in ("private SQL", "fixture-token", "fixture-body",
                                                       "fixture-code", "fixture-state", "existing-login"))


def test_session_touch_is_throttled_without_caching_revocation(pg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    actor = uuid4()
    session_hash = main._sha256("fixture-session")
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'touch_actor','fixture')",
               (actor,))
    pg.execute("INSERT INTO web_sessions(session_hash,authorization_id,expires_at,last_seen_at) "
               "VALUES(%s,%s,NOW()+INTERVAL '1 day',NOW()-INTERVAL '10 minutes')", (session_hash, actor))
    request = _request_with_session("fixture-session")
    authorization = main._current_authorization(request)
    assert authorization["status"] == "active" and authorization["broadcast_enabled"] is True
    touched = pg.execute("SELECT last_seen_at FROM web_sessions WHERE session_hash=%s", (session_hash,)).fetchone()
    pg.execute("UPDATE web_sessions SET last_seen_at=NOW()-INTERVAL '1 minute' WHERE session_hash=%s", (session_hash,))
    for _ in range(3):
        assert main._current_authorization(request)["id"] == actor
    unchanged = pg.execute("SELECT last_seen_at FROM web_sessions WHERE session_hash=%s", (session_hash,)).fetchone()
    assert touched["last_seen_at"] - unchanged["last_seen_at"] == timedelta(minutes=1)
    pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (actor,))
    assert main._current_authorization(request)["broadcast_enabled"] is False
    pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor,))
    assert main._current_authorization(request)["status"] == "revoked"
    pg.execute("DELETE FROM web_sessions WHERE session_hash=%s", (session_hash,))
    assert main._current_authorization(request) is None


def test_dashboard_navigation_preserves_access_and_connection_controls(pg, monkeypatch) -> None:
    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(main.assistant, "configured", lambda _: True)
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _: "fixture-csrf")
    actor = uuid4()
    authorization = pg.execute("""INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,
        body_consent_at,p2p_discovery_status) VALUES(%s,'new_cm','CM','fixture',NOW(),'succeeded')
        RETURNING *""", (actor,)).fetchone()
    monkeypatch.setattr(creator_broadcast, "get_settings",
                        lambda: SimpleNamespace(oauth_scopes=authorization["scopes"]))
    assert authorization["broadcast_enabled"] and not pg.execute(
        "SELECT 1 FROM admin_roles WHERE authorization_id=%s", (actor,)).fetchone()
    assert not set(creator_broadcast.SEND_SCOPES) & set(authorization["scopes"])
    assert creator_broadcast._scope(pg, actor, str(actor)) == actor
    with pytest.raises(PermissionError):
        creator_broadcast._scope(pg, actor, str(uuid4()))
    context = {
        "authorization": authorization, "admin_role": None, "body_consent": True,
        "stats": {
            "badge": "success", "status": "接入完成", "detail": "当前不会自动发送消息。",
            "last_synced_at": "2026-09-01 12:00 BST", "job": {"status": "succeeded"},
        },
    }
    request = _request_with_session("fixture-session")
    rendered = main._template(request, "dashboard.html", context).body.decode()
    assert "工作台" in rendered and "权限与数据使用" in rendered
    assert 'id="operations-assistant"' in rendered and 'id="oa-launcher"' in rendered
    assert 'href="/creators"' in rendered and 'href="/dashboard/offhours-summary"' in rendered
    assert 'href="/creators/broadcast"' in rendered and 'href="/admin"' not in rendered
    assert 'hx-trigger="every 10s [!document.hidden]"' in rendered
    assert 'hx-trigger="load' not in rendered and 'class="ui-spinner"' not in rendered
    pg.execute("UPDATE authorizations SET broadcast_enabled=FALSE WHERE id=%s", (actor,))
    disabled_authorization = pg.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
    disabled = main._template(request, "dashboard.html", {
        **context, "authorization": disabled_authorization,
    }).body.decode()
    assert "/creators/broadcast" not in disabled and 'href="/admin"' not in disabled
    with pytest.raises(PermissionError):
        creator_broadcast._scope(pg, actor, str(actor))
    owner = main._template(request, "dashboard.html", {
        **context, "authorization": disabled_authorization, "admin_role": "owner", "admin_landing_path": "/admin",
    }).body.decode()
    assert 'href="/creators/broadcast"' in owner and 'href="/admin"' in owner
    admin_context = {**context, 'admin_role': 'owner', 'admin_role_label': 'Owner',
        'admin_permissions': {'messages'}, 'active_page': 'messages', 'users': [],
        'user_page': {'page': 1}, 'selected_user': None}
    admin_page = main._template(request, 'admin/messages.html', admin_context).body.decode()
    assert 'id="operations-assistant"' in admin_page and 'id="oa-cm"' in admin_page
    member_page = main._template(request, 'admin/messages.html', {
        **admin_context, 'admin_role': 'member',
    }).body.decode()
    assert 'id="operations-assistant"' not in member_page
    restricted = main._template(request, "dashboard.html", {
        **context, "authorization": {**disabled_authorization, "body_consent_at": None},
        "body_consent": False, "offhours_summary_visible": False,
    }).body.decode()
    assert "/creators/broadcast" not in restricted and "/dashboard/offhours-summary" not in restricted
    assert "重新使用 Lark 授权" in restricted
    assert 'id="operations-assistant"' not in restricted
    assert "business_nav" not in context
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


def test_dashboard_polling_distinguishes_active_sync_from_scheduled_or_paused_work(pg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    actor, job = uuid4(), uuid4()
    pg.execute("INSERT INTO authorizations(id,open_id,access_token_ciphertext) VALUES(%s,'dashboard_actor','fixture')",
               (actor,))
    pg.execute("INSERT INTO chats(chat_id) VALUES('dashboard_chat')")
    pg.execute("INSERT INTO authorization_chats(authorization_id,chat_id,backfill_complete,sync_status) "
               "VALUES(%s,'dashboard_chat',TRUE,'succeeded')", (actor,))
    pg.execute("INSERT INTO sync_jobs(id,authorization_id,mode,status) VALUES(%s,%s,'incremental','queued')",
               (job, actor))
    monkeypatch.setattr(main, "_current_authorization", lambda _: {"id": actor})
    client = TestClient(main.app)
    now = datetime.now(UTC)
    for status, scheduled, discovery, http_status, error_code, label, syncing in (
        ("queued", now + timedelta(minutes=5), "succeeded", None, None, "已接入 · 定时同步", False),
        ("queued", now - timedelta(minutes=1), "succeeded", None, None, "准备定时更新", False),
        ("running", now, "succeeded", None, None, "定时更新中", True),
        ("running", now, "pending", None, None, "正在发现会话", True),
        ("running", now, "succeeded", 429, None, "Lark 限流暂停", False),
        ("running", now, "succeeded", None, 99991400, "Lark 限流暂停", False),
    ):
        pg.execute("UPDATE sync_jobs SET status=%s,scheduled_at=%s,discovery_status=%s,last_http_status=%s,"
                   "last_error_code=%s WHERE id=%s", (status, scheduled, discovery, http_status, error_code, job))
        response = client.get("/dashboard/stats")
        assert response.status_code == 200 and label in response.text
        assert ('class="ui-spinner"' in response.text) is syncing
        assert 'hx-trigger="every 10s [!document.hidden]"' in response.text
        assert 'hx-trigger="load' not in response.text


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




def test_oauth_reauthorization_defers_creator_scans_for_a_large_existing_roster(pg, monkeypatch):  # noqa: F811
    scopes = ("offline_access", "im:chat:read", "im:message:readonly", "im:chat.members:read")
    settings = Settings(
        database_url="fixture", lark_app_id="fixture-app", lark_app_secret="fixture-secret",
        lark_redirect_uri="https://testserver/auth/lark/callback", token_encryption_key=b"o" * 32,
        oauth_authorize_url="https://lark.test/authorize", oauth_token_url="https://lark.test/token",
        user_info_url="https://lark.test/user", lark_api_base_url="https://lark.test", oauth_scopes=scopes,
        sync_lookback_days=0, sync_interval_seconds=300, sync_overlap_seconds=600,
        member_count_refresh_seconds=86400, worker_idle_seconds=3,
    )
    cipher = TokenCipher(settings.token_encryption_key)
    actor, person, job = uuid4(), uuid4(), uuid4()
    pg.execute("INSERT INTO contacts(id,display_name) VALUES(%s,'Creator')", (person,))
    pg.execute("""INSERT INTO authorizations(id,open_id,display_name,access_token_ciphertext,scopes,
               body_consent_at) VALUES(%s,'ou_bulk_cm','CM',%s,%s,NOW()-INTERVAL '30 days')""",
               (actor, cipher.encrypt("old-access"), list(scopes)))
    pg.execute("""INSERT INTO sync_jobs(id,authorization_id,mode,status,discovery_generation)
               VALUES(%s,%s,'backfill','running',9)""", (job, actor))
    pg.execute("""INSERT INTO chats(chat_id,chat_mode)
               SELECT 'oc_bulk_'||lpad(n::text,4,'0'),'group' FROM generate_series(1,2933) n""")
    pg.execute("""INSERT INTO authorization_chats(authorization_id,chat_id,sync_generation,sync_status,
               backfill_complete,backfill_page_token,incremental_page_token,incremental_job_id,
               member_snapshot_status,member_snapshot_checked_at)
               SELECT %s,chat_id,17,'succeeded',TRUE,'backfill-cursor','incremental-cursor',%s,
                 'succeeded',NOW() FROM chats""", (actor, job))
    pg.execute("UPDATE authorization_chats SET recent_coverage_start_at=NOW()-INTERVAL '7 days' "
               "WHERE authorization_id=%s", (actor,))
    pg.execute("""INSERT INTO creator_importance_profiles(contact_id,responsible_authorization_id,sources)
               VALUES(%s,%s,'[{"chat_id":"oc_bulk_0001"}]')""", (person, actor))
    pg.execute("""INSERT INTO creator_authorization_rescans(authorization_id,generation,stage,
               contact_cursor,chat_cursor,not_before,last_error_code)
               VALUES(%s,7,'chats',%s,'old-chat',NOW()+INTERVAL '1 day','old-error')""", (actor, person))
    pg.execute("""UPDATE ai_feature_configs SET enabled=TRUE,api_key_ciphertext=%s,key_suffix='test'
               WHERE feature IN ('creator_handle_matching','creator_group_classification')""",
               (cipher.encrypt("fixture-key"),))
    # A synchronous authorization scan or snapshot-reset signal must fail this callback.
    for signature, return_type in (
        ("enqueue_creator_importance(target UUID, cm UUID, reset BOOLEAN DEFAULT FALSE, "
         "message_chat TEXT DEFAULT NULL, message_id TEXT DEFAULT NULL)", "UUID"),
        ("creator_handle_chat_signal(chat TEXT)", "VOID"),
        ("enqueue_creator_group(target TEXT, origin TEXT DEFAULT 'incremental', force BOOLEAN DEFAULT FALSE)", "UUID"),
    ):
        pg.execute(f"CREATE OR REPLACE FUNCTION {signature} RETURNS {return_type} LANGUAGE plpgsql "
                   "AS $$ BEGIN RAISE EXCEPTION 'creator scan ran inside OAuth'; END $$")

    class FakeLark:
        def __init__(self, received):
            assert received == settings

        def authorization_url(self, state, challenge):
            assert challenge
            return settings.oauth_authorize_url + "?" + urlencode({"state": state})

        def exchange_code(self, code, verifier):
            assert code == "fixture-code" and verifier
            return SimpleNamespace(access_token="new-access", refresh_token="new-refresh", expires_in=3600,
                                   refresh_expires_in=86400, scope=" ".join(scopes))

        def user_info(self, token):
            assert token == "new-access"
            return LarkProfile(open_id="ou_bulk_cm", union_id=None, name="CM", en_name=None, avatar_url=None)

    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "LarkClient", FakeLark)
    client = TestClient(main.app, base_url="https://testserver")
    started = client.get("/auth/lark/start", follow_redirects=False)
    assert started.status_code == 303
    state = client.cookies[main.OAUTH_STATE_COOKIE]
    assert parse_qs(urlparse(started.headers["location"]).query)["state"] == [state]
    response = client.get("/auth/lark/callback", params={"state": state, "code": "fixture-code"},
                          follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"
    session = response.cookies[main.SESSION_COOKIE]
    assert pg.execute("SELECT authorization_id FROM web_sessions WHERE session_hash=%s",
                      (main._sha256(session),)).fetchone()["authorization_id"] == actor
    assert not pg.execute("SELECT 1 FROM oauth_states").fetchone()
    pending = pg.execute("SELECT * FROM creator_authorization_rescans WHERE authorization_id=%s",
                         (actor,)).fetchone()
    assert pending["generation"] == 8 and pending["stage"] == "importance"
    assert pending["contact_cursor"] is pending["chat_cursor"] is None
    assert pending["last_error_code"] == "" and pending["not_before"] <= datetime.now(UTC)
    chats = pg.execute("SELECT * FROM authorization_chats WHERE authorization_id=%s", (actor,)).fetchall()
    assert len(chats) == 2933
    for chat in chats:
        assert chat["sync_generation"] == 18 and not chat["backfill_complete"]
        assert chat["sync_status"] == chat["member_snapshot_status"] == "pending"
        assert chat["recent_coverage_start_at"] is None
        assert all(chat[field] is None for field in ("backfill_page_token", "incremental_page_token",
                                                    "incremental_job_id", "member_snapshot_checked_at"))
    queued = pg.execute("SELECT * FROM sync_jobs WHERE id=%s", (job,)).fetchone()
    assert queued["status"] == "queued" and queued["mode"] == "backfill" and queued["discovery_generation"] == 10


@pytest.mark.parametrize("upgrade,owner,initial_status,broadcast_enabled", (
    ("profile", True, "attention_required", False),
    ("send", False, "attention_required", True),
    ("media", False, "active", True),
))
def test_profile_scope_upgrade_preserves_history_and_rejects_partial_or_overlapping_grants(
    pg, monkeypatch, caplog, upgrade, owner, initial_status, broadcast_enabled,  # noqa: F811
):
    scopes = (
        "offline_access", "im:chat:read", "im:message:readonly", "im:chat.members:read",
        "contact:contact.base:readonly", "contact:user.base:readonly",
    )
    send = upgrade in {"send", "media"}
    initial_scopes = scopes + ("contact:user.email:readonly",) if send else scopes[:4]
    upgrade_scopes = tuple(dict.fromkeys((*scopes, *initial_scopes, *main.SEND_OAUTH_SCOPES))) if send else scopes
    if upgrade == "media":
        upgrade_scopes += ("im:resource",)
    settings = Settings(
        database_url="fixture", lark_app_id="fixture-app", lark_app_secret="fixture-secret",
        lark_redirect_uri="https://testserver/auth/lark/callback", token_encryption_key=b"o" * 32,
        oauth_authorize_url="https://lark.test/authorize", oauth_token_url="https://lark.test/token",
        user_info_url="https://lark.test/user", lark_api_base_url="https://lark.test", oauth_scopes=scopes,
        sync_lookback_days=0, sync_interval_seconds=300, sync_overlap_seconds=600,
        member_count_refresh_seconds=86400, worker_idle_seconds=3,
    )
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
        VALUES(%s,'ou_upgrade','Old Profile',%s,%s,%s,%s,NOW()-INTERVAL '30 days',
        'succeeded',NOW()-INTERVAL '1 day','retained p2p detail')""",
        (actor, cipher.encrypt("old-access"), list(initial_scopes), initial_status,
         "token_expired" if send and initial_status == "attention_required" else
         None if send else main.PROFILE_SCOPE_UPGRADE_REASON),
    )
    if owner:
        pg.execute("INSERT INTO admin_roles(authorization_id,role) VALUES(%s,'owner')", (actor,))
    pg.execute("UPDATE authorizations SET broadcast_enabled=%s WHERE id=%s", (broadcast_enabled, actor))
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
    pg.execute("UPDATE authorization_chats SET recent_coverage_start_at=NOW()-INTERVAL '7 days' "
               "WHERE authorization_id=%s", (actor,))
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

    provider_calls = {"exchange": 0, "profile": 0}

    class FakeLark:
        def __init__(self, received):
            self.scopes = received.oauth_scopes
            assert received == replace(settings, oauth_scopes=self.scopes)

        def authorization_url(self, state, challenge):
            assert state and challenge
            return settings.oauth_authorize_url + "?" + urlencode({"scope": " ".join(self.scopes)})

        def exchange_code(self, code, verifier):
            provider_calls["exchange"] += 1
            assert verifier == "synthetic-verifier"
            if code == "api_error":
                raise LarkApiError(503, 999999, "new-access-full raw-body-secret")
            granted = (
                None if code == "missing" else " ".join(scopes[:4]) if code == "partial" else
                " ".join(initial_scopes) if code == "missing_send" else
                " ".join(scopes) if code == "ordinary" else " ".join(upgrade_scopes)
            )
            return SimpleNamespace(
                access_token="new-access-" + code,
                refresh_token=None if code == "no_refresh" else "new-refresh", expires_in=3600,
                refresh_expires_in=86400, scope=granted,
            )

        def user_info(self, token):
            provider_calls["profile"] += 1
            assert token.startswith("new-access-")
            return replace(profile, open_id="ou_counterpart") if token.endswith("wrong_account") else profile

    monkeypatch.setattr(main, "connection", lambda: nullcontext(pg))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "LarkClient", FakeLark)
    client = TestClient(main.app, base_url="https://testserver")

    def state(*, fresh=False, upgraded=True):
        value = uuid4().hex
        # The fixture holds one transaction/NOW(); model separate request timestamps explicitly.
        pg.execute(
            """INSERT INTO oauth_states(state_hash,code_verifier_ciphertext,expires_at,created_at,
            requested_scopes,authorization_id)
            VALUES(%s,%s,NOW()+INTERVAL '10 minutes',NOW()+(%s*INTERVAL '1 second'),%s,%s)""",
            (main._sha256(value), cipher.encrypt("synthetic-verifier"), 1 if fresh else -60,
             list(upgrade_scopes) if send and upgraded else None, actor if send and upgraded else None),
        )
        return value

    current = None

    def callback(value, code="full", *, session_actor=actor):
        nonlocal current
        if send:
            current = pg.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
            current = {**current, "id": session_actor} if session_actor else None
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
    if send:
        # Any already-consenting CM may upgrade only their own account, without widening onboarding.
        monkeypatch.setattr(main, "_current_authorization", lambda _: current)
        for authorization in (None, {"id": actor, "status": "revoked", "body_consent_at": True},
                              {"id": actor, "status": "active", "body_consent_at": None}):
            current = authorization
            assert client.get("/auth/lark/start?send=1", follow_redirects=False).status_code == 403
        assert not pg.execute("SELECT 1 FROM oauth_states").fetchone()
        for status in ("active", "attention_required"):
            pg.execute("UPDATE authorizations SET status=%s WHERE id=%s", (status, actor))
            current = pg.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
            before_start = snapshot()
            for account_id in (str(uuid4()), "", "not-a-uuid"):
                assert client.get("/auth/lark/start", params={"send": "1", "account_id": account_id},
                                  follow_redirects=False).status_code == 403
                assert not pg.execute("SELECT 1 FROM oauth_states").fetchone()
            for params, requested, target in (
                ({"send": "1", **({"media": "1"} if upgrade == "media" else {})}, upgrade_scopes, actor),
                ({"send": "1", "account_id": str(actor), **({"media": "1"} if upgrade == "media" else {})},
                 upgrade_scopes, actor),
                ({"send": "1", "media": "1", "account_id": str(actor)},
                 tuple(dict.fromkeys((*upgrade_scopes, "im:resource"))), actor),
                ({}, scopes, None),
            ):
                response = client.get("/auth/lark/start", params=params, follow_redirects=False)
                assert response.status_code == 303
                assert parse_qs(urlparse(response.headers["location"]).query)["scope"] == [" ".join(requested)]
                row = pg.execute("SELECT * FROM oauth_states").fetchone()
                assert row["requested_scopes"] == list(requested) and row["authorization_id"] == target
                pg.execute("DELETE FROM oauth_states")
                assert settings.oauth_scopes == scopes and snapshot() == before_start
            assert client.get("/auth/lark/start?media=1", follow_redirects=False).status_code == 400
        pg.execute("UPDATE authorizations SET status=%s WHERE id=%s", (initial_status, actor))
        assert snapshot() == original
        current = pg.execute("SELECT * FROM authorizations WHERE id=%s", (actor,)).fetchone()
        client.cookies.clear()
    overlap = state()
    for grant in ("missing", "partial"):
        assert callback(state(), grant).status_code == 403
        assert snapshot() == original
    if send:
        for session_actor in (None, uuid4()):
            assert callback(state(), session_actor=session_actor).status_code == 403
            assert snapshot() == original
        for grant, status in (("missing_send", 403), ("wrong_account", 403), ("no_refresh", 502)):
            assert callback(state(), grant).status_code == status
            assert snapshot() == original
        # Revocation after link creation must not reactivate or replace the account.
        revoked_link = state()
        pg.execute("UPDATE authorizations SET status='revoked' WHERE id=%s", (actor,))
        revoked = snapshot()
        assert callback(revoked_link).status_code == 403 and snapshot() == revoked
        pg.execute("UPDATE authorizations SET status=%s WHERE id=%s", (initial_status, actor))

    real_audit = main._audit
    audit_attempts = 0

    def fail_after_audit(conn, authorization_id, action):
        nonlocal audit_attempts
        real_audit(conn, authorization_id, action)
        audit_attempts += 1
        raise failure("private provider error, token=new-access-full, raw-body-secret")

    if send and broadcast_enabled:
        invalid_verifier = state()
        pg.execute("UPDATE oauth_states SET code_verifier_ciphertext=%s WHERE state_hash=%s",
                   (TokenCipher(b"z" * 32).encrypt("synthetic-verifier"), main._sha256(invalid_verifier)))
        before_calls = provider_calls.copy()
        invalid_response = callback(invalid_verifier)
        assert invalid_response.status_code == 502 and snapshot() == original
        assert provider_calls == before_calls and "stage=code_verifier" in caplog.text
        provider_error = callback(state(), "api_error")
        assert provider_error.status_code == 502 and snapshot() == original
        assert provider_error.headers["x-request-id"] in provider_error.text
        assert "stage=token_exchange lark_code=999999" in caplog.text
        assert provider_calls == {"exchange": before_calls["exchange"] + 1, "profile": before_calls["profile"]}
        # Exhausted conflicts and unknown connection outcomes cannot leave partial writes or leak errors.
        for failure, attempts in ((psycopg.errors.DeadlockDetected, 3), (psycopg.OperationalError, 1)):  # noqa: B007
            audit_attempts = 0
            monkeypatch.setattr(main, "_audit", fail_after_audit)
            before_calls = provider_calls.copy()
            rejected = callback(state())
            assert rejected.status_code == 503 and "重新授权" in rejected.text
            assert f"/auth/lark/start?send=1&amp;account_id={actor}" in rejected.text
            assert rejected.headers["cache-control"] == "no-store"
            assert rejected.headers["x-request-id"] in rejected.text
            assert main.SESSION_COOKIE not in rejected.cookies
            assert audit_attempts == attempts and snapshot() == original
            assert provider_calls == {key: count + 1 for key, count in before_calls.items()}

    audit_attempts = 0

    def fail_first_audit(conn, authorization_id, action):
        nonlocal audit_attempts
        real_audit(conn, authorization_id, action)
        audit_attempts += 1
        if audit_attempts == 1:
            raise psycopg.errors.DeadlockDetected("token=new-access-full, raw-body-secret")

    monkeypatch.setattr(main, "_audit", fail_first_audit)
    before_calls = provider_calls.copy()
    response = callback(state())
    assert audit_attempts == 2
    assert provider_calls == {key: count + 1 for key, count in before_calls.items()}
    assert "oauth_database_retry" in caplog.text and "stage=profile_provider" in caplog.text
    assert "new-access-full" not in caplog.text and "raw-body-secret" not in caplog.text
    monkeypatch.setattr(main, "_audit", real_audit)
    destination = "/creators/broadcast" if send and (owner or broadcast_enabled) else "/dashboard"
    assert response.status_code == 303 and response.headers["location"] == destination
    upgraded = snapshot()
    auth = upgraded["authorizations"][0]
    assert auth["id"] == actor and auth["open_id"] == "ou_upgrade" and auth["status"] == "active"
    assert auth["broadcast_enabled"] == broadcast_enabled
    assert auth["last_error"] is None and auth["scopes"] == list(upgrade_scopes)
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
    assert provider["scopes"] == list(upgrade_scopes)
    assert provider["status"] == "pending" and provider["error_code"] is None
    if send:
        assert upgraded["sync_jobs"] == original["sync_jobs"]
    else:
        assert upgraded["sync_jobs"][0]["id"] == job and upgraded["sync_jobs"][0]["mode"] == "incremental"
    session = response.cookies[main.SESSION_COOKIE]
    assert upgraded["web_sessions"][0]["session_hash"] == main._sha256(session)
    assert upgraded["web_sessions"][0]["authorization_id"] == actor
    assert [row["action"] for row in upgraded["audit_events"] if row["authorization_id"] == actor] == [
        "oauth_send_permissions_upgraded" if send else "oauth_profile_permissions_upgraded"
    ]

    assert callback(overlap).status_code == 409
    assert snapshot() == upgraded  # An older tab cannot reset cursors or replace tokens/profile again.
    if send:
        pg.execute("UPDATE authorizations SET status='attention_required' WHERE id=%s", (actor,))
        waiting = snapshot()
        assert callback(state()).status_code == 409 and snapshot() == waiting
        pg.execute("UPDATE authorizations SET status='active' WHERE id=%s", (actor,))
        assert snapshot() == upgraded

    ordinary_response = callback(state(fresh=True, upgraded=False), "ordinary")
    assert ordinary_response.status_code == 303
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

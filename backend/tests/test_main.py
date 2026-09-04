from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import contact_assignments, main


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


def test_dashboard_is_reminder_focused_and_transparent() -> None:
    rendered = main.templates.get_template("dashboard.html").render(
        authorization={"display_name": "CM", "p2p_discovery_status": "succeeded"},
        admin_role=None,
        body_consent=True,
        creator_features_enabled=False,
        creator_workbench={"pending_candidates": [], "confirmed_creators": []},
        csrf_token="csrf",
        stats={
            "badge": "success",
            "status": "接入完成",
            "detail": "提醒规则正在测试中；当前不会自动发送消息。",
            "last_synced_at": "2026-09-01 12:00 BST",
            "job": {"status": "succeeded"},
        },
    )

    assert "消息提醒中心" in rendered
    assert "权限与数据使用" in rendered
    assert "已同步消息总数" not in rendered
    assert "待我确认的达人候选" not in rendered
    assert "我的达人（正式）" in rendered
    assert "暂无正式负责的达人。" in rendered


def test_dashboard_loads_confirmed_creators_when_ai_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    authorization_id = uuid4()
    monkeypatch.setattr(
        main,
        "_current_authorization",
        lambda _request: {
            "id": authorization_id,
            "status": "active",
            "display_name": "Remi",
            "body_consent_at": "authorized",
            "p2p_discovery_status": "succeeded",
        },
    )
    monkeypatch.setattr(main, "_dashboard_stats", lambda _authorization_id: {})
    monkeypatch.setattr(main, "admin_role_for", lambda _authorization_id: None)
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _authorization: "csrf")
    monkeypatch.setattr(contact_assignments, "assignment_available", lambda: False)
    monkeypatch.setattr(
        contact_assignments,
        "creator_workbench",
        lambda actual_id: {
            "pending_candidates": [],
            "confirmed_creators": [
                {"display_name": "正式达人", "started_at_label": "2026-09-04 12:00 BST"}
            ],
        }
        if actual_id == authorization_id
        else pytest.fail("dashboard queried the wrong authorization"),
    )

    response = main.dashboard(_request_with_session("active-session"))
    body = response.body.decode()

    assert "待我确认的达人候选" not in body
    assert "我的达人（正式）" in body
    assert "正式达人" in body


def test_dashboard_shows_pending_candidates_and_confirmed_creators() -> None:
    assessment_id = uuid4()
    rendered = main.templates.get_template("dashboard.html").render(
        authorization={"display_name": "CM", "p2p_discovery_status": "succeeded"},
        admin_role=None,
        body_consent=True,
        creator_features_enabled=True,
        csrf_token="csrf",
        creator_workbench={
            "pending_candidates": [
                {
                    "assessment_id": str(assessment_id),
                    "display_name": "候选达人",
                    "confidence_label": "96%",
                    "suggested_at_label": "2026-09-03 14:00 BST",
                }
            ],
            "confirmed_creators": [
                {"display_name": "正式达人", "started_at_label": "2026-09-03 15:00 BST"}
            ],
        },
        stats={
            "badge": "success",
            "status": "接入完成",
            "detail": "提醒规则正在测试中；当前不会自动发送消息。",
            "last_synced_at": "2026-09-03 15:00 BST",
            "job": {"status": "succeeded"},
        },
    )

    assert "待我确认的达人候选" in rendered
    assert "候选达人" in rendered
    assert "96%" in rendered
    assert f'/dashboard/creator-candidates/{assessment_id}/confirm' in rendered
    assert 'name="csrf_token" value="csrf"' in rendered
    assert "我的达人（正式）" in rendered
    assert "正式达人" in rendered


def test_candidate_confirmation_rejects_missing_inactive_or_non_csrf_sessions(monkeypatch) -> None:
    assessment_id = uuid4()
    actor_id = uuid4()
    confirmed = []
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _authorization: "csrf")
    monkeypatch.setattr(
        contact_assignments,
        "confirm_suggested_assignment",
        lambda *args: confirmed.append(args),
    )
    client = TestClient(main.app)

    monkeypatch.setattr(main, "_current_authorization", lambda _request: None)
    no_session = client.post(
        f"/dashboard/creator-candidates/{assessment_id}/confirm",
        data={"csrf_token": "csrf"},
        follow_redirects=False,
    )

    monkeypatch.setattr(
        main,
        "_current_authorization",
        lambda _request: {"id": actor_id, "status": "attention_required"},
    )
    inactive = client.post(
        f"/dashboard/creator-candidates/{assessment_id}/confirm",
        data={"csrf_token": "csrf"},
        follow_redirects=False,
    )

    monkeypatch.setattr(
        main,
        "_current_authorization",
        lambda _request: {"id": actor_id, "status": "active"},
    )
    bad_csrf = client.post(
        f"/dashboard/creator-candidates/{assessment_id}/confirm",
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    )

    assert (no_session.status_code, no_session.headers["location"]) == (303, "/")
    assert inactive.status_code == 403
    assert bad_csrf.status_code == 400
    assert inactive.headers["cache-control"] == bad_csrf.headers["cache-control"] == "no-store, max-age=0"
    assert confirmed == []


def test_candidate_confirmation_uses_only_the_session_actor(monkeypatch) -> None:
    assessment_id = uuid4()
    actor_id = uuid4()
    confirmed = []
    monkeypatch.setattr(
        main,
        "_current_authorization",
        lambda _request: {"id": actor_id, "status": "active"},
    )
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _authorization: "csrf")
    monkeypatch.setattr(
        contact_assignments,
        "confirm_suggested_assignment",
        lambda *args: confirmed.append(args),
    )

    response = TestClient(main.app).post(
        f"/dashboard/creator-candidates/{assessment_id}/confirm",
        data={"csrf_token": "csrf", "responsible_authorization_id": str(uuid4())},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert confirmed == [(actor_id, assessment_id)]


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

import pytest
from starlette.requests import Request

from app import main


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

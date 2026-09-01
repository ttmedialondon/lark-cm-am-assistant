from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_reporting


def _client(current_authorization, contexts=None):
    app = FastAPI()

    def render(_request, name, context, status_code=200):
        if contexts is not None:
            contexts.append((name, context))
        return PlainTextResponse("rendered", status_code=status_code)

    app.include_router(
        admin.create_admin_router(
            current_authorization=current_authorization,
            render_template=render,
        )
    )
    return TestClient(app)


def test_admin_redirects_when_the_browser_has_no_session():
    response = _client(lambda _request: None).get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_admin_fails_closed_without_an_active_admin_role(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: None)
    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin")

    assert response.status_code == 403


def test_active_owner_can_reach_the_overview(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "overview",
        lambda: {"metrics": (), "attention": (), "recent_jobs": (), "runtime": ()},
    )

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin")

    assert response.status_code == 200


def test_message_content_page_is_owner_only(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "operator")

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/messages")

    assert response.status_code == 403


def test_selecting_a_user_lists_conversations_without_reading_message_content(monkeypatch):
    owner_id = uuid4()
    selected_id = uuid4()
    contexts = []
    content_reads = []
    audits = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda: [{"id": selected_id, "display_name": "CM", "body_consent_at": object()}],
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *_args: [{"chat_id": "oc_visible", "chat_mode": "group"}],
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_page",
        lambda *_args: content_reads.append(_args),
    )
    monkeypatch.setattr(admin_reporting, "record_message_view", lambda *_args: audits.append(_args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        f"/admin/messages?user_id={selected_id}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert contexts[-1][1]["selected_chat"] is None
    assert content_reads == []
    assert audits == []


def test_owner_reads_only_an_explicit_visible_conversation(monkeypatch):
    owner_id = uuid4()
    selected_id = uuid4()
    page_reads = []
    audits = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda: [{"id": selected_id, "display_name": "CM", "body_consent_at": object()}],
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *_args: [{"chat_id": "oc_visible", "chat_mode": "p2p"}],
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_page",
        lambda *_args: page_reads.append(_args) or {"metrics": {}, "items": [], "page": 1, "has_next": False},
    )
    monkeypatch.setattr(admin_reporting, "record_message_view", lambda *_args: audits.append(_args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).get(
        f"/admin/messages?user_id={selected_id}&chat_id=oc_visible"
    )

    assert response.status_code == 200
    assert page_reads == [(selected_id, "p2p", "oc_visible", 1)]
    assert audits == [(owner_id, selected_id, "oc_visible")]


def test_unknown_conversation_does_not_fall_back_to_all_messages(monkeypatch):
    owner_id = uuid4()
    selected_id = uuid4()
    page_reads = []
    audits = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda: [{"id": selected_id, "display_name": "CM", "body_consent_at": object()}],
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *_args: [{"chat_id": "oc_visible", "chat_mode": "group"}],
    )
    monkeypatch.setattr(admin_reporting, "message_page", lambda *_args: page_reads.append(_args))
    monkeypatch.setattr(admin_reporting, "record_message_view", lambda *_args: audits.append(_args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).get(
        f"/admin/messages?user_id={selected_id}&chat_id=oc_not_visible"
    )

    assert response.status_code == 200
    assert page_reads == []
    assert audits == []

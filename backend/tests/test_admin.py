from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_reporting, creator_registry


def _page(items, page=1, has_next=False):
    return {"items": items, "page": page, "has_next": has_next}


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
            csrf_token=lambda _authorization: "csrf",
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


def test_admin_users_uses_server_pagination(monkeypatch):
    contexts = []
    pages = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda page: pages.append(page) or _page([], page=page, has_next=True),
    )

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}, contexts).get("/admin/users?page=2")

    assert response.status_code == 200
    assert pages == [2]
    assert contexts[-1][1]["user_page"]["previous_url"] == "/admin/users?page=1"
    assert contexts[-1][1]["user_page"]["next_url"] == "/admin/users?page=3"


def test_message_content_page_is_owner_only(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "operator")

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/messages")

    assert response.status_code == 403


def test_creator_library_is_owner_only_and_uses_the_owner_context(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(admin_reporting, "creators", lambda *_args: _page([]))
    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/creators")

    assert response.status_code == 200


def test_creator_candidates_are_owner_only(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "operator")

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/candidates")

    assert response.status_code == 403


def test_owner_candidate_detail_reads_only_that_candidate_and_audits(monkeypatch):
    owner_id = uuid4()
    candidate_id = uuid4()
    contexts = []
    reads = []
    audits = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "creator_candidate_detail",
        lambda *args: reads.append(args) or {"candidate": {}, "evidence": _page([])},
    )
    monkeypatch.setattr(admin_reporting, "record_candidate_view", lambda *args: audits.append(args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        f"/admin/candidates/{candidate_id}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert reads == [(candidate_id, 1)]
    assert audits == [(owner_id, candidate_id)]
    assert contexts[-1][0] == "admin/candidate_detail.html"


def test_creator_write_requires_csrf_and_redirects_after_a_valid_post(monkeypatch):
    owner_id = uuid4()
    creator_id = uuid4()
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(creator_registry, "create_creator", lambda *_args: creator_id)
    client = _client(lambda _request: {"id": owner_id, "status": "active"})

    assert client.post("/admin/creators", data={"display_name": "Creator"}).status_code == 400
    response = client.post(
        "/admin/creators",
        data={"display_name": "Creator", "csrf_token": "csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/creators/{creator_id}"


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
        lambda *_args: _page([{"id": selected_id, "display_name": "CM", "body_consent_at": object()}]),
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *_args: _page([{"chat_id": "oc_visible", "chat_mode": "group"}]),
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


def test_message_conversation_search_and_type_stay_with_the_selected_user(monkeypatch):
    owner_id = uuid4()
    selected_id = uuid4()
    contexts = []
    conversation_queries = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda *_args: _page([{"id": selected_id, "display_name": "CM", "body_consent_at": object()}]),
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *args: conversation_queries.append(args)
        or _page([{"chat_id": "oc_visible", "chat_mode": "p2p"}], page=2, has_next=True),
    )

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        f"/admin/messages?user_id={selected_id}&conversation_page=2"
        "&conversation_query=Creator&conversation_type=p2p"
    )

    assert response.status_code == 200
    assert conversation_queries == [(selected_id, 2, "Creator", "p2p")]
    context = contexts[-1][1]
    assert context["conversation_query"] == "Creator"
    assert context["conversation_type"] == "p2p"
    assert "conversation_query=Creator" in context["conversation_page"]["next_url"]
    assert "conversation_type=p2p" in context["conversation_page"]["next_url"]


def test_owner_reads_only_an_explicit_visible_conversation(monkeypatch):
    owner_id = uuid4()
    selected_id = uuid4()
    page_reads = []
    audits = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda *_args: _page([{"id": selected_id, "display_name": "CM", "body_consent_at": object()}]),
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *_args: _page([{"chat_id": "oc_visible", "chat_mode": "p2p"}]),
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversation",
        lambda *_args: {"chat_id": "oc_visible", "chat_mode": "p2p"},
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
    assert page_reads == [(selected_id, "oc_visible", 1)]
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
        lambda *_args: _page([{"id": selected_id, "display_name": "CM", "body_consent_at": object()}]),
    )
    monkeypatch.setattr(
        admin_reporting,
        "message_conversations",
        lambda *_args: _page([{"chat_id": "oc_visible", "chat_mode": "group"}]),
    )
    monkeypatch.setattr(admin_reporting, "message_conversation", lambda *_args: None)
    monkeypatch.setattr(admin_reporting, "message_page", lambda *_args: page_reads.append(_args))
    monkeypatch.setattr(admin_reporting, "record_message_view", lambda *_args: audits.append(_args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).get(
        f"/admin/messages?user_id={selected_id}&chat_id=oc_not_visible"
    )

    assert response.status_code == 200
    assert page_reads == []
    assert audits == []

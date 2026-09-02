from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_reporting, contact_assessments


def _page(items, page=1, has_next=False, **metadata):
    return {"items": items, "page": page, "has_next": has_next, **metadata}


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


def test_admin_users_numbered_pagination_keeps_edge_pages(monkeypatch):
    contexts = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda page: _page([], page=page, total_count=850, total_pages=17, page_size=50),
    )
    client = _client(lambda _request: {"id": uuid4(), "status": "active"}, contexts)

    assert client.get("/admin/users?page=1").status_code == 200
    first = contexts[-1][1]["user_page"]
    assert [item.get("page", "…") for item in first["pages"]] == [1, 2, 3, "…", 17]

    assert client.get("/admin/users?page=8").status_code == 200
    middle = contexts[-1][1]["user_page"]
    assert [item.get("page", "…") for item in middle["pages"]] == [1, "…", 7, 8, 9, "…", 17]
    assert middle["pages"][-1]["url"] == "/admin/users?page=17"


def test_message_content_page_is_owner_only(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "operator")

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/messages")

    assert response.status_code == 403


def test_creator_library_is_owner_only_and_uses_the_owner_context(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(admin_reporting, "creators", lambda *_args: _page([]))
    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/creators")

    assert response.status_code == 200


def test_contact_directory_is_owner_only(monkeypatch):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "operator")

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/contacts")

    assert response.status_code == 403


def test_contact_directory_filters_confirmation_and_keeps_it_in_pagination(monkeypatch):
    owner_id = uuid4()
    contexts = []
    calls = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "contacts",
        lambda page, confirmation: calls.append((page, confirmation))
        or _page([], page=page, total_count=50, total_pages=2, page_size=25),
    )

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        "/admin/contacts?page=2&confirmation=confirmed"
    )

    assert response.status_code == 200
    assert calls == [(2, "confirmed")]
    assert contexts[-1][1]["confirmation"] == "confirmed"
    assert contexts[-1][1]["contact_page"]["previous_url"] == "/admin/contacts?confirmation=confirmed&page=1"


def test_removed_ai_candidate_queue_is_not_routable():
    assert _client(lambda _request: {"id": uuid4(), "status": "active"}).get("/admin/ai-candidates").status_code == 404


def test_owner_contact_detail_reads_only_that_contact_without_message_content(monkeypatch):
    owner_id = uuid4()
    contexts = []
    reads = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "contact_detail",
        lambda *args: reads.append(args)
        or {
            "contact": {"contact_id": uuid4()},
            "identities": [],
            "conversations": _page([]),
        },
    )
    monkeypatch.setattr(contact_assessments, "latest_contact_assessment", lambda _contact_id: None)
    monkeypatch.setattr(contact_assessments, "assessment_available", lambda: True)

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        "/admin/contacts/ou_contact?conversations_page=2"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert reads == [("ou_contact", 2)]
    assert contexts[-1][0] == "admin/contact_detail.html"
    assert contexts[-1][1]["assessment"] is None
    assert contexts[-1][1]["assessment_available"] is True


def test_owner_can_dismiss_one_ai_suggestion_from_its_contact(monkeypatch):
    owner_id = uuid4()
    assessment_id = uuid4()
    dismissed = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(contact_assessments, "assessment_matches_contact", lambda *_args: True)
    monkeypatch.setattr(contact_assessments, "dismiss_assessment", lambda *args: dismissed.append(args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        f"/admin/contacts/ou_contact/assessments/{assessment_id}/dismiss?page=3&confirmation=unconfirmed",
        data={"csrf_token": "csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/contacts/ou_contact?page=3&confirmation=unconfirmed"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert dismissed == [(owner_id, assessment_id)]


def test_owner_can_view_assessment_evidence_and_it_is_audited(monkeypatch):
    owner_id = uuid4()
    assessment_id = uuid4()
    contexts = []
    viewed = []
    detail = {
        "id": assessment_id,
        "contact": {"open_id": "ou_contact", "display_name": "Contact"},
        "status": "succeeded",
        "role_suggestions": [],
        "evidence": [{"label": "M1", "content": "Scoped evidence", "available": True}],
    }
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(contact_assessments, "assessment_matches_contact", lambda *_args: True)
    monkeypatch.setattr(contact_assessments, "assessment_detail", lambda _assessment_id: detail)
    monkeypatch.setattr(contact_assessments, "record_assessment_view", lambda *args: viewed.append(args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        f"/admin/contacts/ou_contact/assessments/{assessment_id}?page=2"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert contexts[-1][0] == "admin/contact_assessment.html"
    assert contexts[-1][1]["assessment"] is detail
    assert viewed == [(owner_id, assessment_id)]


def test_owner_confirmation_uses_only_suggested_form_values(monkeypatch):
    owner_id = uuid4()
    assessment_id = uuid4()
    confirmed = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(contact_assessments, "assessment_matches_contact", lambda *_args: True)
    monkeypatch.setattr(
        contact_assessments,
        "confirm_assessment_role",
        lambda *args: confirmed.append(args),
    )

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        f"/admin/contacts/ou_contact/assessments/{assessment_id}/roles?page=4",
        data={"csrf_token": "csrf", "role": "creator"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/admin/contacts/ou_contact/assessments/{assessment_id}?page=4&confirmation=all"
    )
    assert confirmed == [(owner_id, assessment_id, "creator")]


def test_owner_cannot_confirm_an_assessment_through_another_contact_path(monkeypatch):
    owner_id = uuid4()
    assessment_id = uuid4()
    confirmed = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(contact_assessments, "assessment_matches_contact", lambda *_args: False)
    monkeypatch.setattr(contact_assessments, "confirm_assessment_role", lambda *args: confirmed.append(args))

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        f"/admin/contacts/ou_other/assessments/{assessment_id}/roles",
        data={"csrf_token": "csrf", "role": "creator"},
    )

    assert response.status_code == 404
    assert confirmed == []


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
        or _page(
            [{"chat_id": "oc_visible", "chat_mode": "p2p"}],
            page=2,
            total_count=85,
            total_pages=17,
            page_size=5,
        ),
    )

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        f"/admin/messages?user_id={selected_id}&conversation_page=2&conversation_query=Creator&conversation_type=p2p"
    )

    assert response.status_code == 200
    assert conversation_queries == [(selected_id, 2, "Creator", "p2p")]
    context = contexts[-1][1]
    assert context["conversation_query"] == "Creator"
    assert context["conversation_type"] == "p2p"
    assert "conversation_query=Creator" in context["conversation_page"]["next_url"]
    assert "conversation_type=p2p" in context["conversation_page"]["next_url"]
    assert "conversation_query=Creator" in context["conversation_page"]["pages"][-1]["url"]
    assert "conversation_type=p2p" in context["conversation_page"]["pages"][-1]["url"]


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

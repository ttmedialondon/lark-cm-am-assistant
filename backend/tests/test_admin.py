from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_permissions, admin_reporting, organization_sync


@pytest.fixture(autouse=True)
def legacy_role_scenarios_use_explicit_acl(monkeypatch):
    """Keep these original owner/denied scenarios while using the new live ACL boundary."""
    monkeypatch.setattr(admin_permissions, "access_for", lambda authorization_id: admin_permissions._access({
        "status": "active", "role": admin_reporting.admin_role_for(authorization_id),
        "role_active": True, "permissions": [],
    }))
    monkeypatch.setattr(admin_permissions, "users_access_data", lambda ids: {
        authorization_id: admin_permissions._access(None) for authorization_id in ids
    })


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


def test_admin_users_uses_server_pagination(monkeypatch):
    contexts = []
    pages = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "users",
        lambda page, q, permission_filter: pages.append((page, q, permission_filter))
        or _page([], page=page, has_next=True, summary={}),
    )

    response = _client(lambda _request: {"id": uuid4(), "status": "active"}, contexts).get("/admin/users?page=2")

    assert response.status_code == 200
    assert pages == [(2, "", "all")]
    assert contexts[-1][1]["user_page"]["previous_url"] == "/admin/users?page=1"
    assert contexts[-1][1]["user_page"]["next_url"] == "/admin/users?page=3"


def test_people_directory_filters_name_and_category_without_legacy_organization_filters(monkeypatch):
    owner_id = uuid4()
    contexts = []
    calls = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "contacts",
        lambda page, category, organization_status, organization_query, q: calls.append(
            (page, category, organization_status, organization_query, q)
        )
        or _page([], page=page, total_count=50, total_pages=2, page_size=25),
    )
    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        "/admin/contacts?page=2&category=creator&organization_status=verified&organization_query=TT+MEDIA+ITALY&q=Ada"
    )

    assert response.status_code == 200
    assert calls == [(2, "creator", "all", "", "Ada")]
    assert contexts[-1][1]["category"] == "creator"
    assert contexts[-1][1]["contact_page"]["previous_url"] == (
        "/admin/contacts?category=creator&q=Ada&page=1"
    )
    assert contexts[-1][1]["refresh_url"] == "/admin/contacts?category=creator&page=2&q=Ada"
    assert all("organization" not in item["url"] for item in contexts[-1][1]["category_filters"])


def test_owner_contact_detail_reads_only_that_contact_without_message_content(monkeypatch):
    owner_id = uuid4()
    contexts = []
    reads = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "contact_detail",
        lambda *args, include_conversations: reads.append((*args, include_conversations))
        or {
            "contact": {"contact_id": uuid4()},
            "identities": [],
            "organizations": [],
            "conversations": _page([]),
        },
    )

    response = _client(lambda _request: {"id": owner_id, "status": "active"}, contexts).get(
        "/admin/contacts/ou_contact?conversations_page=2"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert reads == [("ou_contact", 2, True)]
    assert contexts[-1][0] == "admin/contact_detail.html"
    assert "assessment" not in contexts[-1][1]
    assert "assessment_available" not in contexts[-1][1]


def test_owner_can_confirm_one_persons_organization(monkeypatch):
    owner_id = uuid4()
    contact_id = uuid4()
    confirmed = []

    class Result:
        @staticmethod
        def fetchone():
            return {"contact_id": contact_id}

    class Connection:
        @staticmethod
        def execute(_query, _params):
            return Result()

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(admin, "connection", connection)
    monkeypatch.setattr(
        organization_sync,
        "manually_confirm_contact_organization",
        lambda *args, **kwargs: confirmed.append((args, kwargs)),
    )

    response = _client(lambda _request: {"id": owner_id, "status": "active"}).post(
        "/admin/contacts/ou_contact/organization?page=2&category=unclassified&organization_query=TT+MEDIA",
        data={
            "csrf_token": "csrf",
            "canonical_name": "TT MEDIA ITALY",
            "organization_kind": "mcn_or_agency",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin/contacts/ou_contact?page=2&category=unclassified&organization_status=all&organization_query=TT+MEDIA"
    )
    assert confirmed == [
        ((owner_id, contact_id, "ou_contact", "TT MEDIA ITALY", "mcn_or_agency"), {"map_current_tenant": False})
    ]












def test_message_conversation_search_and_type_stay_with_the_selected_user(monkeypatch):
    owner_id = uuid4()
    selected_id = uuid4()
    contexts = []
    conversation_queries = []
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _authorization_id: "owner")
    monkeypatch.setattr(
        admin_reporting,
        "message_users",
        lambda *_args: _page([{"id": selected_id, "display_name": "CM", "body_consent_at": object(),
                              "scopes": ["im:chat:read", "im:message:readonly"]}]),
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

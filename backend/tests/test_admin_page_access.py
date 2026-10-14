"""Every backend entry is independently checked before querying its data."""

import asyncio
import re
from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import (
    admin,
    admin_permissions,
    admin_reporting,
    main,
    organization_sync,
)

TARGET = "0a11485c-20d4-4c0c-83c8-1d43445d11b1"
PAGES = tuple(page["key"] for page in admin_permissions.PAGES)
GET_ROUTES = (
    ("overview", "/admin"),
    ("users", "/admin/users"),
    ("users", f"/admin/users/{TARGET}"),
    ("chats", "/admin/chats"),
    ("messages", "/admin/messages"),
    ("messages", f"/admin/messages/export?user_id={TARGET}"),
    ("contacts", "/admin/contacts"),
    ("contacts", "/admin/contacts/ou_contact"),
    ("ai", "/admin/ai"),
    ("jobs", "/admin/jobs"),
    ("assistant-rules", "/admin/assistant-rules"),
)
POST_ROUTES = (
    f"/admin/users/{TARGET}/permissions",
    "/admin/contacts/ou_contact/organization",
    "/admin/ai", "/admin/ai/resume",
)


class _DataQueryReached(Exception):
    """A permitted route reached its first data query; no database is used."""


@pytest.fixture
def data_fence(monkeypatch):
    def forbidden_query(*_args, **_kwargs):
        raise _DataQueryReached

    for module in (
        admin, admin_permissions, admin_reporting,
        organization_sync,
    ):
        monkeypatch.setattr(module, "connection", forbidden_query)


def _client(monkeypatch, *, permissions=(), owner=False, status="active", contexts=None):
    actor = {"id": uuid4(), "status": status, "display_name": "Test account"}
    state = {"permissions": frozenset(permissions), "owner": owner}

    def access_for(_authorization_id):
        allowed = frozenset(PAGES) if state["owner"] else state["permissions"]
        first = next((page for page in PAGES if page in allowed), None)
        return {
            "is_owner": state["owner"],
            "permissions": allowed,
            "landing_path": "/admin" if first == "overview" else f"/admin/{first}" if first else None,
            "has_admin_access": bool(allowed),
        }

    monkeypatch.setattr(admin_permissions, "access_for", access_for)
    app = FastAPI()

    def render(_request, name, context, status_code=200):
        if contexts is not None:
            contexts.append((name, context))
        return PlainTextResponse("rendered", status_code=status_code)

    def authorization(_request):
        # Session/ACL database work must not block the ASGI event loop.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return actor

    app.include_router(admin.create_admin_router(
        current_authorization=authorization,
        render_template=render,
        csrf_token=lambda _: "csrf",
    ))
    return TestClient(app), actor, state


@pytest.mark.parametrize(("required", "path"), GET_ROUTES)
def test_each_route_checks_its_own_permission_before_reading(monkeypatch, data_fence, required, path):
    browser, _, state = _client(monkeypatch, permissions={required})
    with pytest.raises(_DataQueryReached):
        browser.get(path, follow_redirects=False)
    unrelated = "messages" if required != "messages" else "users"
    state["permissions"] = frozenset({unrelated})
    response = browser.get(path, follow_redirects=False)
    assert response.status_code == (303 if path == "/admin" else 403)
    if path == "/admin":
        assert response.headers["location"] == f"/admin/{unrelated}"


@pytest.mark.parametrize("path", POST_ROUTES)
def test_all_page_grants_still_cannot_mutate_or_grant_access(monkeypatch, data_fence, path):
    browser, _, _ = _client(monkeypatch, permissions=PAGES)
    response = browser.post(path, data={"csrf_token": "csrf", "permissions": list(PAGES)}, follow_redirects=False)
    assert response.status_code == 403


def test_document_management_is_narrowly_delegated_and_revocable(monkeypatch, data_fence):
    browser, _, state = _client(monkeypatch, permissions={'assistant-rules'})
    form = {'csrf_token': 'csrf', 'document': '', 'revision': '0', 'title': 'Sample guide',
            'description': 'Sample questions', 'kind': 'manual', 'status': 'draft',
            'visibility': 'owner', 'content': 'Check actual facts.'}
    assert browser.post('/admin/assistant-rules', data={**form, 'csrf_token': 'wrong'}).status_code == 400
    with pytest.raises(_DataQueryReached):
        browser.post('/admin/assistant-rules', data=form)
    for path in POST_ROUTES:
        assert browser.post(path, data=form).status_code == 403
    state['permissions'] = frozenset({'users'})
    assert browser.get('/admin/assistant-rules').status_code == 403
    assert browser.post('/admin/assistant-rules', data=form).status_code == 403


def test_revocation_applies_to_the_next_request_in_the_same_session(monkeypatch, data_fence):
    browser, _, state = _client(monkeypatch, permissions={"messages"})
    with pytest.raises(_DataQueryReached):
        browser.get("/admin/messages")
    state["permissions"] = frozenset()
    assert browser.get("/admin/messages").status_code == 403


@pytest.mark.parametrize("owner", [False, True])
def test_inactive_authorization_has_no_backend_access(monkeypatch, data_fence, owner):
    browser, _, _ = _client(monkeypatch, permissions=PAGES, owner=owner, status="revoked")
    for path in [path for _, path in GET_ROUTES]:
        assert browser.get(path, follow_redirects=False).status_code == 403



@pytest.mark.parametrize(("permissions", "owner", "expected"), [
    ((), False, set()),
    (("messages",), False, {"/admin/messages"}),
    (("assistant-rules",), False, {"/admin/assistant-rules"}),
    ((), True, {"/admin"}),
])
def test_frontend_entry_uses_current_page_permissions(monkeypatch, permissions, owner, expected):
    monkeypatch.setattr(main, "connection", lambda: nullcontext(None))
    monkeypatch.setattr(main.assistant, "configured", lambda _: True)
    _, actor, _ = _client(monkeypatch, permissions=permissions, owner=owner)
    actor.update(body_consent_at=True, p2p_discovery_status="succeeded")
    monkeypatch.setattr(main, "_current_authorization", lambda _: actor)
    monkeypatch.setattr(main, "_dashboard_stats", lambda _: {
        "badge": "success", "status": "已同步", "detail": "已完成", "last_synced_at": "—",
        "job": {"status": "succeeded"},
    })
    monkeypatch.setattr(main, "_admin_csrf_token", lambda _: "csrf")
    response = TestClient(main.app).get("/dashboard")
    assert response.status_code == 200
    backend_links = {
        link for link in re.findall(r'href="([^"]+)"', response.text) if link.startswith("/admin")
    }
    assert backend_links == expected


@pytest.mark.parametrize("keys", [[], ["users", "messages"]])
def test_owner_saves_repeated_checkbox_values_and_can_clear_all(monkeypatch, data_fence, keys):
    saved = []
    browser, actor, _ = _client(monkeypatch, owner=True)
    monkeypatch.setattr(admin_permissions, "set_permissions", lambda *args: saved.append(args))
    response = browser.post(
        f"/admin/users/{TARGET}/permissions",
        data={"csrf_token": "csrf", "permissions": keys, "page": "2", "q": "Remi"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "page=2" in response.headers["location"]
    assert "q=Remi" in response.headers["location"]
    assert saved == [(actor["id"], UUID(TARGET), keys)]


@pytest.mark.parametrize("body", [
    b"permissions=users",
    b"csrf_token=wrong&permissions=users",
    b"csrf_token=csrf&csrf_token=csrf&permissions=users",
    b"csrf_token=csrf&role=owner",
    b"csrf_token=csrf&permissions=unknown-page",
    b"csrf_token=csrf&page=invalid",
    b"csrf_token=csrf&q=\xff",
    b"csrf_token=csrf&" + b"permissions=users&" * 21,
    b"csrf_token=csrf&q=" + b"x" * 4096,
])
def test_invalid_permission_forms_never_reach_a_database_write(monkeypatch, data_fence, body):
    browser, _, _ = _client(monkeypatch, owner=True)
    response = browser.post(
        f"/admin/users/{TARGET}/permissions",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(("extra", "conversations", "jobs"), [
    ((), False, False), (("chats",), True, False), (("messages",), True, False),
    (("jobs",), False, True), (("chats", "jobs"), True, True),
])
def test_personnel_detail_only_requests_authorized_extra_panels(monkeypatch, data_fence, extra, conversations, jobs):
    calls, contexts = [], []
    browser, _, _ = _client(monkeypatch, permissions={"users", *extra}, contexts=contexts)

    def user_detail(*args, **kwargs):
        calls.append(kwargs)
        page = {"items": [], "page": 1, "has_next": False}
        return {"user": {"id": UUID(TARGET)}, "conversations": page, "jobs": page}

    monkeypatch.setattr(admin_reporting, "user_detail", user_detail)
    assert browser.get(f"/admin/users/{TARGET}").status_code == 200
    assert calls == [{"include_conversations": conversations, "include_jobs": jobs}]
    assert contexts[-1][1]["can_view_conversations"] is conversations
    assert contexts[-1][1]["can_view_jobs"] is jobs

"""Every backend entry is independently checked before querying its data."""

import asyncio
import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

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
)
PRIVATE_ROUTES = ()
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


@pytest.mark.parametrize("path", [path for _, path in GET_ROUTES] + list(PRIVATE_ROUTES))
def test_no_page_grants_means_no_backend_even_with_a_legacy_role(monkeypatch, data_fence, path):
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _: "operator")
    browser, _, _ = _client(monkeypatch)
    assert browser.get(path, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("path", POST_ROUTES)
def test_all_page_grants_still_cannot_mutate_or_grant_access(monkeypatch, data_fence, path):
    browser, _, _ = _client(monkeypatch, permissions=PAGES)
    response = browser.post(path, data={"csrf_token": "csrf", "permissions": list(PAGES)}, follow_redirects=False)
    assert response.status_code == 403


def test_contact_assessment_plaintext_remains_owner_only(monkeypatch, data_fence):
    browser, _, _ = _client(monkeypatch, permissions=PAGES)
    for path in PRIVATE_ROUTES:
        assert browser.get(path).status_code == 403


@pytest.mark.parametrize("path", [path for _, path in GET_ROUTES] + list(PRIVATE_ROUTES))
def test_owner_has_every_read_entry_without_explicit_page_grants(monkeypatch, data_fence, path):
    browser, _, _ = _client(monkeypatch, owner=True)
    with pytest.raises(_DataQueryReached):
        browser.get(path, follow_redirects=False)


def test_revocation_applies_to_the_next_request_in_the_same_session(monkeypatch, data_fence):
    browser, _, state = _client(monkeypatch, permissions={"messages"})
    with pytest.raises(_DataQueryReached):
        browser.get("/admin/messages")
    state["permissions"] = frozenset()
    assert browser.get("/admin/messages").status_code == 403


@pytest.mark.parametrize("owner", [False, True])
def test_inactive_authorization_has_no_backend_access(monkeypatch, data_fence, owner):
    browser, _, _ = _client(monkeypatch, permissions=PAGES, owner=owner, status="revoked")
    for _, path in GET_ROUTES:
        assert browser.get(path, follow_redirects=False).status_code == 403



def _page_menu_targets(html):
    menu = re.search(r'<nav\b[^>]*aria-label="页面导航"[^>]*>(.*?)</nav>', html, re.DOTALL)
    assert menu is not None
    return re.findall(r'href="([^"]+)"', menu.group(1))


def _assert_no_navigation_role_labels(html):
    for label in ("系统主人", "全部权限", "已授权查看", "当前为只读访问", "权限设置及业务修改"):
        assert label not in html



def test_owner_navigation_exposes_every_page():
    environment = Environment(loader=FileSystemLoader(Path(admin.__file__).parent / "templates"), autoescape=True)
    html = environment.get_template("admin/_nav.html").render(
        authorization={"display_name": "System owner"},
        admin_role="owner",
        admin_role_label="系统主人",
        admin_permissions=frozenset(PAGES),
        permission_pages=admin_permissions.PAGES,
        admin_landing_path="/admin",
        active_page="users",
    )
    backend_links = {link for link in re.findall(r'href="([^"]+)"', html) if link.startswith("/admin")}
    assert backend_links == {page["path"] for page in admin_permissions.PAGES if page["path"].startswith("/admin")}
    assert _page_menu_targets(html) == list(dict.fromkeys(page["path"] for page in admin_permissions.PAGES))
    _assert_no_navigation_role_labels(html)


@pytest.mark.parametrize("permissions", [
    ("users", "messages"),
    ("ai", "jobs"),
])
def test_navigation_keeps_exact_grants_when_switching_between_pages(permissions):
    environment = Environment(loader=FileSystemLoader(Path(admin.__file__).parent / "templates"), autoescape=True)
    expected = [page["path"] for page in admin_permissions.PAGES if page["key"] in permissions]
    for active_page in permissions:
        html = environment.get_template("admin/_nav.html").render(
            authorization={"display_name": "Page viewer"}, admin_role="member", admin_role_label="已授权查看",
            admin_permissions=frozenset(permissions), permission_pages=admin_permissions.PAGES,
            admin_landing_path=expected[0], active_page=active_page,
        )
        assert _page_menu_targets(html) == expected
        _assert_no_navigation_role_labels(html)
        assert "Page viewer" in html and "/dashboard" in html
        assert "/admin/creators" not in html and "/admin/creator-review" not in html


@pytest.mark.parametrize(("permissions", "owner", "expected"), [
    ((), False, set()),
    (("messages",), False, {"/admin/messages"}),
    ((), True, {"/admin"}),
])
def test_frontend_entry_uses_current_page_permissions(monkeypatch, permissions, owner, expected):
    monkeypatch.setattr(main.creator_broadcast, "preview_open", lambda: False)
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


@pytest.mark.parametrize(("page", "template", "details"), [
    ("overview", "overview.html", {
        "metrics": [], "attention": [{"id": TARGET, "display_name": "Other person"}],
        "recent_jobs": [], "runtime": [],
    }),
    ("contacts", "contact_detail.html", {
        "contact": {"open_id": "ou_creator", "primary_role": "creator", "confirmed_role_labels": []},
        "source_query": "", "assessment": {"status": "succeeded", "id": TARGET},
        "organizations": [], "identities": [], "conversations": [], "can_view_conversations": False,
    }),
    ("ai", "ai.html", {
        "ai_dispatch": {"state": "paused"},
        "configs": [{"feature": "contact_assessment", "config": {"key_configured": True, "api_key_label": "…1234"}}],
    }),
])
def test_page_templates_hide_ungranted_links_and_owner_forms(page, template, details):
    environment = Environment(loader=FileSystemLoader(Path(admin.__file__).parent / "templates"), autoescape=True)
    landing = "/admin" if page == "overview" else f"/admin/{page}"
    html = environment.get_template(f"admin/{template}").render(
        authorization={"display_name": "Page viewer"}, admin_role="member", admin_role_label="已授权查看",
        admin_permissions=frozenset({page}), permission_pages=admin_permissions.PAGES,
        admin_landing_path=landing, active_page=page, **details,
    )
    assert re.search(r'<form\b[^>]*\bmethod="post"', html, re.IGNORECASE) is None
    backend_links = {link.split("?", 1)[0] for link in re.findall(r'href="([^"]+)"', html) if link.startswith("/admin")}
    assert all(link == landing or link.startswith(f"{landing}/") for link in backend_links)
    if page == "overview":
        assert backend_links == {landing}
    if page == "contacts":
        assert "/assessments/" not in " ".join(backend_links)


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


def test_every_admin_route_is_in_the_access_matrix(monkeypatch):
    browser, _, _ = _client(monkeypatch)
    actual = set()
    for route in browser.app.routes:
        if not route.path.startswith("/admin"):
            continue
        path = route.path.replace("{open_id}", "ou_contact")
        for parameter in ("authorization_id", "contact_id", "assessment_id", "run_id", "task_id"):
            path = path.replace(f"{{{parameter}}}", TARGET)
        actual.update((method, path) for method in route.methods)
    expected = {("GET", path.split("?", 1)[0]) for _, path in GET_ROUTES}
    expected.update(("GET", path) for path in PRIVATE_ROUTES)
    expected.update(("POST", path) for path in POST_ROUTES)
    assert actual == expected


@pytest.mark.parametrize("actor_owner", [False, True])
@pytest.mark.parametrize("target_owner_active", [None, False, True])
def test_permission_editor_is_owner_only_and_never_edits_an_owner(actor_owner, target_owner_active):
    environment = Environment(loader=FileSystemLoader(Path(admin.__file__).parent / "templates"), autoescape=True)
    target_access = admin_permissions._access({
        "status": "active", "role": "owner" if target_owner_active is not None else None,
        "role_active": bool(target_owner_active), "permissions": ["messages"] if target_owner_active is False else [],
    })
    html = environment.get_template("admin/_user_permissions.html").render(
        admin_role="owner" if actor_owner else "member", permission_access=target_access,
        user={"id": TARGET, "display_name": "<script>unsafe name</script>"},
        permission_pages=admin_permissions.PAGES, permission_page=1, csrf_token="csrf",
    )
    should_edit = actor_owner and target_owner_active is None
    assert bool(re.search(r'<form\b[^>]*\bmethod="post"', html)) is should_edit
    assert "<script>" not in html
    if target_owner_active is False:
        assert target_access["is_owner_account"]
        assert not target_access["has_admin_access"]
        assert not target_access["permissions"]
    if should_edit:
        assert html.count('name="permissions"') == len(PAGES)
        assert 'name="csrf_token" value="csrf"' in html
        assert ' checked' not in html
        assert "全部不勾选 = 仅前台" in html

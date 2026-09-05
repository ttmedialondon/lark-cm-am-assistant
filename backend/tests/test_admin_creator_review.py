from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from app import admin, admin_permissions, creator_review


def client(monkeypatch, *, role="owner", active=True, contexts=None, permissions=(), html=False):
    actor = {"id": uuid4(), "status": "active" if active else "revoked", "display_name": "PRIVATE_VIEWER_NAME"}
    monkeypatch.setattr(admin_permissions, "access_for", lambda _: admin_permissions._access({
        "status": actor["status"], "role": role, "role_active": True, "permissions": permissions,
    }))
    app = FastAPI()
    environment = Environment(
        loader=FileSystemLoader(Path(admin.__file__).parent / "templates"), autoescape=True,
    )

    def render(_request, name, context, status_code=200):
        if contexts is not None:
            contexts.append((name, context))
        if html:
            return HTMLResponse(environment.get_template(name).render(**context), status_code=status_code)
        return PlainTextResponse("rendered", status_code=status_code)

    app.include_router(admin.create_admin_router(
        current_authorization=lambda _: actor,
        render_template=render,
        csrf_token=lambda _: "csrf",
    ))
    return TestClient(app), actor


@pytest.mark.parametrize("role", [None, "viewer", "operator"])
@pytest.mark.parametrize("path", ["/admin/creator-review", f"/admin/creator-review/{uuid4()}"])
def test_review_preview_rejects_non_owner_before_loading_data(monkeypatch, role, path):
    def must_not_query(*_args, **_kwargs):
        pytest.fail("Restricted data was queried for a non-owner")

    monkeypatch.setattr(creator_review, "directory", must_not_query)
    monkeypatch.setattr(creator_review, "detail", must_not_query)
    browser, _ = client(monkeypatch, role=role)
    assert browser.get(path).status_code == 403


def test_review_preview_rejects_inactive_owner(monkeypatch):
    browser, _ = client(monkeypatch, active=False)
    assert browser.get("/admin/creator-review").status_code == 403


def test_list_preserves_filters_and_uses_review_only_links(monkeypatch):
    contexts = []
    calls = []
    contact_id, responsible_id = uuid4(), uuid4()

    def directory(*args):
        calls.append(args)
        return {
            "items": [{"id": contact_id, "handles": ["sample_creator"]}],
            "page": 2, "page_size": 25, "total_count": 70, "total_pages": 3, "has_next": True,
            "summary": {"creators": 70, "handles": 72, "documented": 60, "needs_review": 10},
            "owners": [{"id": responsible_id, "name": "Owner"}],
        }

    monkeypatch.setattr(creator_review, "directory", directory)
    browser, _ = client(monkeypatch, contexts=contexts)
    response = browser.get(
        "/admin/creator-review",
        params={"page": 2, "q": " sample ", "responsible_id": str(responsible_id), "evidence": "review"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "same-origin"
    assert calls == [(2, "sample", responsible_id, "review")]
    template, context = contexts[-1]
    assert template == "admin/creator_review.html"
    assert context["creators"][0]["detail_url"].startswith(f"/admin/creator-review/{contact_id}?")
    for url in [context["creator_page"]["previous_url"], context["creator_page"]["next_url"]]:
        assert "q=sample" in url and "evidence=review" in url and str(responsible_id) in url


def test_non_creator_detail_is_not_found(monkeypatch):
    monkeypatch.setattr(creator_review, "detail", lambda _: None)
    browser, _ = client(monkeypatch)
    assert browser.get(f"/admin/creator-review/{uuid4()}").status_code == 404


def test_empty_owner_from_search_form_means_all_owners(monkeypatch):
    calls = []
    monkeypatch.setattr(creator_review, "directory", lambda *args: calls.append(args) or {
        "items": [], "page": 1, "has_next": False, "summary": {}, "owners": [],
    })
    browser, _ = client(monkeypatch)
    assert browser.get("/admin/creator-review?q=sample&responsible_id=&evidence=all").status_code == 200
    assert calls == [(1, "sample", None, "all")]
    assert browser.get("/admin/creator-review?responsible_id=invalid").status_code == 400
    assert len(calls) == 1


def test_unlisted_chat_id_cannot_load_or_audit_messages(monkeypatch):
    def must_not_call(*_args):
        pytest.fail("An out-of-scope chat reached message loading or audit")

    monkeypatch.setattr(creator_review, "detail", lambda _: {
        "creator": {"id": uuid4()},
        "conversations": [{"chat_id": "approved-chat", "name": "Creator chat"}],
    })
    monkeypatch.setattr(creator_review, "messages", must_not_call)
    monkeypatch.setattr(creator_review, "record_view", must_not_call)
    browser, _ = client(monkeypatch)
    assert browser.get(f"/admin/creator-review/{uuid4()}?chat_id=other-chat").status_code == 404


def test_message_scope_recheck_failure_aborts_render_and_audit(monkeypatch):
    contexts = []
    audit_calls = []
    monkeypatch.setattr(creator_review, "detail", lambda _: {
        "creator": {"id": uuid4()}, "conversations": [{"chat_id": "approved-chat"}],
    })
    monkeypatch.setattr(creator_review, "messages", lambda *_: None)
    monkeypatch.setattr(creator_review, "record_view", lambda *args: audit_calls.append(args))
    browser, _ = client(monkeypatch, contexts=contexts)
    assert browser.get(f"/admin/creator-review/{uuid4()}?chat_id=approved-chat").status_code == 404
    assert contexts[-1][0] == "error.html"
    assert audit_calls == []


def test_scoped_chat_read_is_audited_and_keeps_creator_pagination(monkeypatch):
    contexts, audit_calls, read_calls = [], [], []
    contact_id = uuid4()
    monkeypatch.setattr(creator_review, "detail", lambda _: {
        "creator": {"id": contact_id},
        "conversations": [{"chat_id": "approved-chat", "name": "Creator chat"}],
    })

    def messages(*args):
        read_calls.append(args)
        return {"items": [], "page": 2, "page_size": 50, "total_count": 120, "total_pages": 3, "has_next": True}

    monkeypatch.setattr(creator_review, "messages", messages)
    monkeypatch.setattr(creator_review, "record_view", lambda *args: audit_calls.append(args))
    browser, actor = client(monkeypatch, contexts=contexts)
    response = browser.get(
        f"/admin/creator-review/{contact_id}",
        params={"page": 4, "q": "sample", "evidence": "documented", "chat_id": "approved-chat", "message_page": 2},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert read_calls == [(contact_id, "approved-chat", 2)]
    assert audit_calls == [(actor["id"], contact_id, "approved-chat")]
    context = contexts[-1][1]
    for url in [context["back_url"], context["message_page"]["next_url"]]:
        assert "page=4" in url and "evidence=documented" in url and "q=sample" in url
    assert "message_page=3" in context["message_page"]["next_url"]
    assert "chat_id=approved-chat" in context["message_page"]["next_url"]
    assert context["message_page"]["next_url"].endswith("#cr-chat-title")
    assert context["detail"]["conversations"][0]["url"].endswith("#cr-chat-title")


def test_review_has_no_mutation_endpoint(monkeypatch):
    browser, _ = client(monkeypatch)
    assert browser.post(f"/admin/creator-review/{uuid4()}", data={"responsible_id": str(uuid4())}).status_code == 405


class _Navigation(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.targets = []
        self.page_targets = []
        self.page_navigation_count = 0
        self._page_nav_depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "nav":
            if attr.get("aria-label") == "页面导航":
                self.page_navigation_count += 1
                self._page_nav_depth = 1
            elif self._page_nav_depth:
                self._page_nav_depth += 1
        if tag == "a" and "href" in attr:
            self.targets.append(attr["href"])
            if self._page_nav_depth:
                self.page_targets.append(attr["href"])
        if tag == "form" and "action" in attr:
            self.targets.append(attr["action"])

    def handle_endtag(self, tag):
        if tag == "nav" and self._page_nav_depth:
            self._page_nav_depth -= 1


def _assert_review_interface(html, permissions=()):
    for private_label in (
        "系统主人", "全部权限", "所有者", "只读预览", "只读查阅", "已获页面授权",
        "管理台", "我的统计", "后台", "PRIVATE_VIEWER_NAME",
    ):
        assert private_label not in html
    navigation = _Navigation(html)
    expected_menu = [page["path"] for page in admin_permissions.PAGES if page["key"] in permissions]
    assert navigation.page_targets == expected_menu
    assert navigation.page_navigation_count == bool(expected_menu)
    targets = navigation.targets
    assert targets
    assert all(
        urlsplit(target).path == "/admin/creator-review"
        or urlsplit(target).path.startswith("/admin/creator-review/")
        or target in expected_menu
        for target in targets
    )
    return targets


@pytest.fixture
def rendered_review_data(monkeypatch):
    contact_id = uuid4()
    creator = {
        "id": contact_id, "handles": ["sample_creator"], "display_name": "Sample Creator",
        "responsible_name": "Remi", "assignment_label": "已关联", "source_label": "人工导入",
        "updated_at_label": "2026-09-05", "evidence_status": "documented",
        "evidence_label": "依据已记录", "evidence_reason": "账号与导入资料已对应",
    }
    conversation = {
        "chat_id": "oc_creator", "name": "Sample creator group", "chat_mode_label": "群聊",
        "message_count": 75, "context_label": "达人参与的会话",
        "last_message_at_label": "2026-09-05", "source_label": "Remi",
    }
    page = {"page": 2, "page_size": 25, "total_count": 75, "total_pages": 3, "has_next": True}
    monkeypatch.setattr(creator_review, "directory", lambda *_: {
        **page, "items": [creator], "owners": [{"id": "remi", "name": "Remi"}],
        "summary": {"creators": 75, "handles": 75, "documented": 75, "needs_review": 0},
    })
    monkeypatch.setattr(creator_review, "detail", lambda _: {
        "creator": creator, "conversations": [conversation], "evidence_steps": [],
        "source_label": "Excel 人工导入", "source_rows_label": "10", "source_owner_label": "Remi",
        "reviewed_at_label": "2026-09-05", "conversation_notice": "仅显示已同步授权范围内的记录。",
    })
    monkeypatch.setattr(creator_review, "messages", lambda *_: {
        **page, "items": [{
            "sent_by_author": False, "sender_label": "Sample Creator", "sender_role_label": "达人",
            "message_type_label": "文字", "created_at_label": "2026-09-05 12:00",
            "content": "Message <script>sample</script>",
        }],
    })
    audit_calls = []
    monkeypatch.setattr(creator_review, "record_view", lambda *args: audit_calls.append(args))
    return contact_id, audit_calls


@pytest.mark.parametrize(("role", "permissions"), [
    ("member", ["creator-review"]),
    ("member", ["creator-review", "creators"]),
    ("member", ["creator-review", "overview", "users", "messages", "jobs"]),
    ("member", ["creator-review", "chats", "contacts", "ai"]),
    ("owner", []),
])
@pytest.mark.parametrize("view", ["list", "detail", "messages"])
def test_review_navigation_exactly_matches_page_grants(monkeypatch, rendered_review_data, role, permissions, view):
    contact_id, audits = rendered_review_data
    browser, actor = client(monkeypatch, role=role, permissions=permissions, html=True)
    path = "/admin/creator-review" if view == "list" else f"/admin/creator-review/{contact_id}"
    params = {"page": 2, "q": "sample", "evidence": "documented"}
    if view == "messages":
        params.update(chat_id="oc_creator", message_page=2)
    response = browser.get(path, params=params)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    expected_permissions = admin_permissions.PAGE_KEYS if role == "owner" else permissions
    targets = _assert_review_interface(response.text, expected_permissions)
    assert "Sample Creator" in response.text and "Remi" in response.text
    if view == "list":
        assert any(urlsplit(target).path == f"/admin/creator-review/{contact_id}" for target in targets)
        assert any(parse_qs(urlsplit(target).query).get("page") == ["3"] for target in targets)
    else:
        assert "Sample creator group" in response.text
        assert "授权来源：Remi" in response.text
        assert any(parse_qs(urlsplit(target).query).get("chat_id") == ["oc_creator"] for target in targets)
    if view == "messages":
        assert "Message &lt;script&gt;sample&lt;/script&gt;" in response.text
        assert any(parse_qs(urlsplit(target).query).get("message_page") == ["3"] for target in targets)
        assert audits == [(actor["id"], contact_id, "oc_creator")]
    else:
        assert audits == []


@pytest.mark.parametrize("role", ["owner", "member"])
@pytest.mark.parametrize("failure", ["invalid_owner", "unknown_creator", "unrelated_chat"])
def test_review_errors_stay_inside_the_review_interface(monkeypatch, rendered_review_data, role, failure):
    contact_id, audits = rendered_review_data
    browser, _ = client(monkeypatch, role=role, permissions=["creator-review"], html=True)
    if failure == "invalid_owner":
        path, expected = "/admin/creator-review?responsible_id=invalid", 400
    elif failure == "unknown_creator":
        monkeypatch.setattr(creator_review, "detail", lambda _: None)
        path, expected = f"/admin/creator-review/{contact_id}", 404
    else:
        path, expected = f"/admin/creator-review/{contact_id}?chat_id=oc_unrelated", 404
    response = browser.get(path)
    assert response.status_code == expected
    _assert_review_interface(response.text)
    assert audits == []


@pytest.mark.parametrize(("role", "active", "permissions"), [
    ("member", True, []), ("owner", False, []), ("member", True, ["jobs"]),
])
@pytest.mark.parametrize("path", ["/admin/creator-review", f"/admin/creator-review/{uuid4()}"])
def test_review_denials_are_neutral_and_do_not_load_private_data(monkeypatch, role, active, permissions, path):
    def must_not_query(*_args, **_kwargs):
        pytest.fail("Denied review access reached private data")

    monkeypatch.setattr(creator_review, "directory", must_not_query)
    monkeypatch.setattr(creator_review, "detail", must_not_query)
    monkeypatch.setattr(creator_review, "messages", must_not_query)
    browser, _ = client(monkeypatch, role=role, active=active, permissions=permissions, html=True)
    response = browser.get(path)
    assert response.status_code == 403
    assert "暂时无法查看" in response.text
    assert "联系页面提供者" in response.text
    _assert_review_interface(response.text)


def test_other_admin_denials_keep_the_existing_error_page(monkeypatch):
    browser, _ = client(monkeypatch, role="member", html=True)
    response = browser.get("/admin/users")
    assert response.status_code == 403
    assert "没有后台页面权限" in response.text
    assert "Lark CM/AM Assistant" in response.text
    assert "返回首页" in response.text
    assert _Navigation(response.text).targets == ["/"]

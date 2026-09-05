from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import admin, admin_reporting, creator_review


def client(monkeypatch, *, role="owner", active=True, contexts=None):
    actor = {"id": uuid4(), "status": "active" if active else "revoked"}
    monkeypatch.setattr(admin_reporting, "admin_role_for", lambda _: role)
    app = FastAPI()

    def render(_request, name, context, status_code=200):
        if contexts is not None:
            contexts.append((name, context))
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

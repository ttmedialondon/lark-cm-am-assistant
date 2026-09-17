"""Message-console state transitions and rendered controls stay inside their source scope."""

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from app import admin, admin_permissions, admin_reporting


def _page(items, number, size, count):
    pages = (count + size - 1) // size
    number = min(max(1, number), max(1, pages))
    return {
        "items": items, "page": number, "page_size": size, "total_count": count,
        "total_pages": pages, "has_next": number < pages,
    }


class _Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = {}
        self.links = []
        self._form = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self._form = values.get("id", "")
            self.forms[self._form] = {"attrs": values, "controls": {}}
        elif self._form is not None and tag in {"input", "select"} and "name" in values:
            self.forms[self._form]["controls"][values["name"]] = values
        elif tag == "a" and "href" in values:
            self.links.append(values["href"])

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


@pytest.fixture
def console(monkeypatch):
    actor = {"id": uuid4(), "display_name": "Page viewer", "status": "active"}
    source = admin_reporting._decorate_user({
        "id": uuid4(), "display_name": "Remi", "authorization_status": "active",
        "body_consent_at": datetime(2026, 9, 1, tzinfo=UTC), "group_count": 2, "p2p_count": 1,
    })
    chat = admin_reporting._decorate_message_conversation({
        "chat_id": "oc_selected", "name": "Creator collaboration", "chat_mode": "group",
        "external": True, "member_count": 3, "member_count_status": "succeeded",
        "message_count": 250, "content_count": 250,
    })
    state = {
        "sources": [source], "source_count": 75, "detail_source": source,
        "chats": [chat], "chat_count": 60, "selected_chat": chat,
        "messages": [{
            "sent_by_author": False, "sender_label": "<Creator>", "message_type": "text",
            "message_type_label": "文字", "created_at_label": "2026-09-05 10:00",
            "created_date_label": "2026-09-05", "content": "Safe <script>message</script>",
        }],
        "content_count": 250,
    }
    calls = {name: [] for name in ("users", "user_detail", "directory", "chat", "messages", "audit")}
    contexts = []
    monkeypatch.setattr(admin_permissions, "access_for", lambda _: admin_permissions._access({
        "status": "active", "role": None, "role_active": False, "permissions": ["messages"],
    }))

    def users(number):
        calls["users"].append(number)
        return _page(state["sources"], number, 25, state["source_count"])

    def user_detail(*args, **kwargs):
        calls["user_detail"].append((args, kwargs))
        return {"user": state["detail_source"]} if state["detail_source"] else None

    def directory(*args):
        calls["directory"].append(args)
        return _page(state["chats"], args[1], 20, state["chat_count"])

    def conversation(*args):
        calls["chat"].append(args)
        return state["selected_chat"]

    def messages(*args):
        calls["messages"].append(args)
        return {
            **_page(state["messages"], args[2], 100, state["content_count"]),
            "metrics": {"message_count": 250, "content_count": state["content_count"], "own_message_count": 30},
        }

    monkeypatch.setattr(admin_reporting, "users", users)
    monkeypatch.setattr(admin_reporting, "user_detail", user_detail)
    monkeypatch.setattr(admin_reporting, "message_conversations", directory)
    monkeypatch.setattr(admin_reporting, "message_conversation", conversation)
    monkeypatch.setattr(admin_reporting, "message_page", messages)
    monkeypatch.setattr(admin_reporting, "record_message_view", lambda *args: calls["audit"].append(args))
    environment = Environment(loader=FileSystemLoader(Path(admin.__file__).parent / "templates"), autoescape=True)

    def render(_request, name, context, status_code=200):
        contexts.append(context)
        return HTMLResponse(environment.get_template(name).render(**context), status_code=status_code)

    app = FastAPI()
    app.include_router(admin.create_admin_router(
        current_authorization=lambda _: actor, render_template=render, csrf_token=lambda _: "csrf",
    ))
    return SimpleNamespace(
        browser=TestClient(app), source=source, chat=chat, actor=actor, state=state, calls=calls, contexts=contexts,
    )


def _open(console, **params):
    return console.browser.get("/admin/messages", params={"user_id": str(console.source["id"]), **params})


def _query(url):
    assert urlsplit(url).path == "/admin/messages"
    return parse_qs(urlsplit(url).query)


def test_initial_console_does_not_choose_a_source_or_read_messages(console):
    response = console.browser.get("/admin/messages")
    assert response.status_code == 200
    assert "先选择一位人员" in response.text
    assert console.contexts[-1]["selected_user"] is None
    assert all(not console.calls[name] for name in ("directory", "chat", "messages", "audit"))


def test_empty_source_directory_has_its_own_state(console):
    console.state.update(sources=[], source_count=0)
    response = console.browser.get("/admin/messages")
    assert response.status_code == 200
    assert "还没有已接入人员" in response.text
    assert console.calls["messages"] == console.calls["audit"] == []


def test_selecting_a_source_does_not_automatically_open_its_first_chat(console):
    response = _open(console)
    assert response.status_code == 200
    assert "选择一个会话开始阅读" in response.text
    assert console.calls["directory"] == [(console.source["id"], 1, "", "all")]
    assert console.calls["chat"] == console.calls["messages"] == console.calls["audit"] == []


@pytest.mark.parametrize(("status", "consent"), [("revoked", True), ("active", False)])
def test_unavailable_source_cannot_query_chats_messages_or_audit(console, status, consent):
    console.source["authorization_status"] = status
    if not consent:
        console.source["body_consent_at"] = None
    response = _open(console, chat_id=console.chat["chat_id"])
    assert response.status_code == 200
    assert "这个来源暂不可用" in response.text
    assert console.contexts[-1]["source_unavailable_reason"]
    assert "可以浏览会话" not in response.text
    assert all(not console.calls[name] for name in ("directory", "chat", "messages", "audit"))


@pytest.mark.parametrize(("query", "title"), [("", "还没有可查看的会话"), ("unmatched", "没有匹配的会话")])
def test_empty_directory_distinguishes_no_chats_from_no_search_results(console, query, title):
    console.state.update(chats=[], chat_count=0)
    response = _open(console, conversation_query=query)
    assert response.status_code == 200
    assert title in response.text
    assert console.calls["messages"] == console.calls["audit"] == []


def test_unavailable_requested_chat_has_an_explicit_state_and_never_falls_back(console):
    console.state["selected_chat"] = None
    response = _open(console, chat_id="oc_unavailable")
    assert response.status_code == 200
    assert "这个会话暂不可用" in response.text
    assert console.contexts[-1]["requested_chat_unavailable"] is True
    assert console.contexts[-1]["selected_chat"] is None
    assert console.calls["messages"] == console.calls["audit"] == []


def test_source_outside_current_user_page_loads_only_metadata(console):
    console.state["sources"] = [{**console.source, "id": uuid4(), "display_name": "Another CM"}]
    response = _open(console, user_page=2)
    assert response.status_code == 200
    assert console.calls["user_detail"] == [(
        (console.source["id"],), {"include_conversations": False, "include_jobs": False},
    )]
    assert console.contexts[-1]["selected_user_in_page"] is False
    assert 'selected>Remi' in response.text or 'selected="selected">Remi' in response.text


def test_unknown_source_is_not_replaced_with_another_person(console):
    console.state["detail_source"] = None
    response = console.browser.get("/admin/messages", params={"user_id": str(uuid4())})
    assert response.status_code == 404
    assert all(not console.calls[name] for name in ("directory", "chat", "messages", "audit"))


def test_message_page_grantee_can_read_and_is_recorded_as_the_actual_viewer(console):
    response = _open(console, chat_id=console.chat["chat_id"])
    assert response.status_code == 200
    assert "仅所有者" not in response.text and "系统主人" not in response.text
    assert "Safe &lt;script&gt;message&lt;/script&gt;" in response.text
    assert "&lt;Creator&gt;" in response.text
    assert console.contexts[-1]["selected_chat_in_page"] is True
    assert console.calls["messages"] == [(console.source["id"], console.chat["chat_id"], 1)]
    assert console.calls["audit"] == [(console.actor["id"], console.source["id"], console.chat["chat_id"])]
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "same-origin"


def test_source_restriction_filters_directory_and_blocks_forged_source(console, monkeypatch):
    allowed = [console.source["id"]]
    monkeypatch.setattr(admin_permissions, "access_for", lambda _: admin_permissions._access({
        "status": "active", "permissions": ["messages"],
        "message_source_ids": allowed,
    }))
    response = console.browser.get("/admin/messages", params={"user_page": 99})
    assert response.status_code == 200
    assert console.contexts[-1]["users"] == [console.source]
    assert console.contexts[-1]["user_page"]["total_count"] == 1
    assert console.calls["users"] == []
    response = _open(console, chat_id=console.chat["chat_id"])
    assert response.status_code == 200
    assert console.calls["audit"] == [(console.actor["id"], console.source["id"], console.chat["chat_id"])]
    counts = {key: len(value) for key, value in console.calls.items()}
    response = console.browser.get("/admin/messages", params={
        "user_id": str(uuid4()), "chat_id": console.chat["chat_id"], "page": 2,
    })
    assert response.status_code == 403
    assert {key: len(value) for key, value in console.calls.items()} == counts
    assert response.headers["cache-control"] == "no-store, max-age=0"
    sources = {console.source["id"]: console.source}
    for name in ("Nero", "Janette", "Jenette Z", "Mona"):
        source_id = uuid4()
        sources[source_id] = {**console.source, "id": source_id, "display_name": name}
        allowed.append(source_id)
    monkeypatch.setattr(admin_reporting, "user_detail", lambda source_id, **_: {"user": sources[source_id]})
    response = console.browser.get("/admin/messages")
    assert response.status_code == 200
    assert {u["id"] for u in console.contexts[-1]["users"]} == set(allowed)
    assert console.contexts[-1]["user_page"]["total_count"] == 5
    for source_id in allowed:
        response = _open(console, user_id=str(source_id), chat_id=console.chat["chat_id"])
        assert response.status_code == 200
        assert console.calls["messages"][-1][0] == source_id
    revoked = allowed.pop()
    counts = {key: len(value) for key, value in console.calls.items()}
    assert _open(console, user_id=str(revoked)).status_code == 403
    assert _open(console, user_id=str(uuid4())).status_code == 403
    assert {key: len(value) for key, value in console.calls.items()} == counts


def test_source_and_filter_forms_reset_dependent_selection_state(console):
    response = _open(console, chat_id=console.chat["chat_id"], user_page=2, conversation_page=2,
                     page=2, conversation_query="creator", conversation_type="group")
    controls = _Controls(response.text)
    source_form = controls.forms["message-source-form"]["controls"]
    assert "user_id" in source_form
    assert not {"chat_id", "page", "conversation_page", "conversation_query", "conversation_type"} & source_form.keys()
    filter_form = controls.forms["conversation-filter-form"]["controls"]
    assert filter_form["user_id"]["value"] == str(console.source["id"])
    assert {"conversation_query", "conversation_type"} <= filter_form.keys()
    assert not {"chat_id", "page", "conversation_page"} & filter_form.keys()


def test_links_and_pagination_preserve_only_their_correct_state(console):
    response = _open(console, chat_id=console.chat["chat_id"], user_page=2, conversation_page=2,
                     page=2, conversation_query="creator", conversation_type="group")
    assert response.status_code == 200
    context = console.contexts[-1]
    shared = {
        "user_id": [str(console.source["id"])], "chat_id": [console.chat["chat_id"]],
        "user_page": ["2"], "conversation_page": ["2"],
        "conversation_query": ["creator"], "conversation_type": ["group"],
    }
    chat_query = _query(context["conversations"][0]["url"])
    assert chat_query == shared
    assert urlsplit(context["conversations"][0]["url"]).fragment == "message-panel"
    assert _query(context["latest_messages_url"]) == {**shared, "page": ["1"]}
    assert _query(context["clear_conversation_filters_url"]) == {
        "user_id": [str(console.source["id"])], "user_page": ["2"],
    }
    assert _query(context["message_page"]["next_url"]) == {**shared, "page": ["3"]}
    assert _query(context["conversation_page"]["next_url"]) == {
        **shared, "conversation_page": ["3"], "page": ["2"],
    }
    assert _query(context["user_page"]["next_url"]) == {**shared, "user_page": ["3"], "page": ["2"]}


def test_current_chat_outside_directory_page_remains_visible_and_is_explained(console):
    console.state["chats"] = [{**console.chat, "chat_id": "oc_other", "name": "Another conversation"}]
    response = _open(console, chat_id=console.chat["chat_id"], conversation_page=2)
    assert response.status_code == 200
    assert console.contexts[-1]["selected_chat_in_page"] is False
    assert "当前会话不在左侧本页" in response.text
    assert "Creator collaboration" in response.text and "Safe &lt;script&gt;message&lt;/script&gt;" in response.text


def test_indexed_conversation_without_saved_content_does_not_guess_the_cause(console):
    console.state.update(messages=[], content_count=0)
    response = _open(console, chat_id=console.chat["chat_id"])
    assert response.status_code == 200
    assert "暂无已存正文" in response.text
    assert "尚未回填完成，或" not in response.text
    assert console.contexts[-1]["selected_chat"]["chat_id"] == console.chat["chat_id"]


def test_chronological_content_is_grouped_by_date_and_names_the_selected_cm(console):
    sample = console.state["messages"][0]
    console.state["messages"] = [
        {**sample, "created_date_label": "2026-09-04", "content": "Older sample text"},
        {**sample, "sent_by_author": True, "sender_label": "本人", "content": "Newer sample text"},
    ]
    response = _open(console, chat_id=console.chat["chat_id"])
    assert response.status_code == 200
    assert response.text.index("Older sample text") < response.text.index("Newer sample text")
    assert re.findall(r'class="message-console__date"><span>([^<]*)</span>', response.text) == [
        "2026-09-04", "2026-09-05",
    ]
    assert re.search(r'message-console__message-meta">\s*<strong>Remi</strong>', response.text)
    assert not re.search(r'message-console__message-meta">\s*<strong>本人</strong>', response.text)
    assert "本页按时间从早到晚" in response.text


def test_time_navigation_labels_and_latest_anchor_match_the_page_direction(console):
    response = _open(console, chat_id=console.chat["chat_id"], page=2)
    context = console.contexts[-1]
    controls = _Controls(response.text)
    assert response.status_code == 200
    assert "更新记录" in response.text and "更早记录" in response.text and "回到最新" in response.text
    assert _query(context["message_page"]["previous_url"])["page"] == ["1"]
    assert _query(context["message_page"]["next_url"])["page"] == ["3"]
    assert context["latest_messages_url"] in controls.links
    assert urlsplit(context["latest_messages_url"]).fragment == "message-latest"
    assert 'id="message-latest"' in response.text

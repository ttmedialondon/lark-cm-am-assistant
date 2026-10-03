"""Read-only HTTP and template boundaries; backend scope checks have their own SQL tests."""

import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin, ai_feature_config
from app import offhours_summary_routes as routes

APP = Path(__file__).resolve().parents[1] / "app"


def render(data):
    data = {"selection": {"cms": [], "dates": [], "date": "2026-09-15", "period": "work"}, **data}
    env = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    return env.get_template("offhours_summary.html").render(
        authorization={"display_name": "Tester"}, summary_data=data,
    )


def render_admin(role="owner"):
    env = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    configs = [{
        **feature, "feature": name, "id_suffix": name.replace("_", "-"),
        "config": ai_feature_config.display_config({"feature": name, "enabled": False, "key_suffix": "test"}),
    } for name, feature in admin.AI_FEATURES.items()]
    return env.get_template("admin/ai.html").render(
        authorization={"display_name": "Fixture Reviewer"}, admin_role=role,
        active_page="ai", admin_landing_path="/admin/ai", admin_permissions=["ai"],
        permission_pages=[{"key": "ai", "path": "/admin/ai", "label": "AI 配置"}],
        ai_dispatch={"state": "ready"}, configs=configs, csrf_token="fixture-csrf",
        summary_settings={"status_label": "未开启", "window_start_input": "2026-09-09T18:00",
                          "window_end_input": "2026-09-10T12:00"},
    )


def test_admin_summary_form_has_native_window_inputs_and_disabled_default_without_identity_threshold():
    html = render_admin()
    forms = re.findall(r'<form action="/admin/ai".*?</form>', html, re.S)
    assert 'name="feature" value="contact_assessment"' not in html
    assert 'name="auto_confirm_threshold"' not in html
    assert 'name="feature" value="creator_import"' in forms[0]
    assert 'name="enabled"' not in forms[0]
    summary_form = next(form for form in forms if 'name="feature" value="offhours_summary"' in form)
    assert 'name="csrf_token" value="fixture-csrf"' in summary_form
    assert ('type="datetime-local" id="summary-window-start" name="window_start" value="2026-09-09T18:00"'
            in summary_form)
    assert 'type="datetime-local" id="summary-window-end" name="window_end" value="2026-09-10T12:00"' in summary_form
    assert 'name="auto_confirm_threshold"' not in summary_form
    assert 'name="enabled" value="1" checked' not in summary_form
    assert "保存配置（开启则立即执行）" in summary_form
    secret_field = re.search(r'<input[^>]*name="api_key"[^>]*>', summary_form).group(0)
    assert 'type="password"' in secret_field
    assert ' value=' not in secret_field
    matching_form = next(form for form in forms if 'name="feature" value="creator_handle_matching"' in form)
    assert 'name="enabled" value="1"' in matching_form
    assert 'name="enabled" value="1" checked' not in matching_form
    assert 'name="window_start"' not in matching_form and 'name="window_end"' not in matching_form
    assert 'name="rpm_limit" value="30"' in matching_form
    assert 'name="tpm_limit"' not in matching_form
    assert 'name="daily_token_budget"' not in matching_form
    assert 'href="/admin/ai/creator-handle-matching"' in html


def test_readonly_admin_has_no_save_resume_key_or_window_edit_controls():
    html = render_admin("reviewer")
    assert '<form ' not in html
    assert '<input ' not in html
    assert "只读配置" in html
    assert 'id="offhours-summary"' in html
    assert 'href="/admin/ai/creator-handle-matching"' not in html


def test_matching_template_renders_body_escapes_names_and_distinguishes_historical_operator_suggestions():
    environment = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    hostile = "<script>alert(1)</script>"
    html = environment.get_template("admin/creator_handle_matching.html").render(
        authorization={"display_name": "Fixture Reviewer"}, admin_landing_path="/admin", admin_role="owner",
        admin_permissions=admin.admin_permissions.PAGE_KEYS, permission_pages=admin.admin_permissions.PAGES,
        active_page="ai", csrf_token="fixture-csrf", matching={
            "enabled": False, "counts": [{"status": "review_required", "n": 1}], "page": 1,
            "jobs": [{"id": uuid4(), "display_name": hostile, "status": "review_required",
                      "reason_code": "suggestions_saved", "input_tokens": 100, "output_tokens": 20,
                      "suggestions": [{"handle": "fixture.handle", "decision": "review", "relation": "operator",
                                       "code": "operates_for_other", "period": "historical"}]}],
        },
    )
    assert "账号建议" in html and 'action="/admin/ai/creator-handle-matching/enqueue"' in html
    assert hostile not in html and "&lt;script&gt;" in html
    assert "代运营" in html and "历史关系" in html and "待核实" in html
    assert "operator" not in html and "review_required" not in html
    assert 'href="https://www.tiktok.com/@fixture.handle"' in html


def test_template_never_exposes_admin_to_a_viewer_and_escapes_private_json():
    hostile = "</script><script>alert(1)</script>"
    html = render({"can_manage": False, "events": [{"title": hostile}]})
    assert 'href="/admin/' not in html
    assert hostile not in html
    raw = re.search(r'<script id="offhours-summary-data" type="application/json">(.*?)</script>', html).group(1)
    assert json.loads(raw)["events"][0]["title"] == hostile
    assert 'href="/admin/ai#offhours-summary"' in render({"can_manage": True})


def test_client_does_not_persist_or_interpolate_messages_as_html():
    script = (APP / "static/offhours-summary.js").read_text()
    for forbidden in ("localStorage", "sessionStorage", "innerHTML", "insertAdjacentHTML"):
        assert forbidden not in script
    assert 'cache: "no-store"' in script
    assert 'credentials: "same-origin"' in script
    assert 'redirect: "error"' in script


@pytest.fixture
def client(monkeypatch):
    auth = {"id": uuid4(), "status": "active"}
    calls = []
    conn = SimpleNamespace(transaction=nullcontext)
    monkeypatch.setattr(routes, "connection", lambda: nullcontext(conn))
    monkeypatch.setattr(
        routes.offhours_summary, "get_summary_view",
        lambda connection, actor, **kwargs: calls.append((connection, actor)) or {"events": []},
    )
    monkeypatch.setattr(routes.summary_periods, "selection",
                        lambda conn, actor, *args: (actor, None, None, {"date": "2026-09-15", "period": "work"}))
    app = FastAPI()
    def authorization(_request):
        import asyncio

        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        auth["session_reads"] = auth.get("session_reads", 0) + 1
        return auth

    app.include_router(routes.create_router(
        current_authorization=authorization,
        render_template=lambda *_: HTMLResponse("page"), csrf_token=lambda _: "test-csrf",
    ))
    return TestClient(app), auth, calls, conn


def test_route_uses_session_identity_not_query_scope_and_is_uncacheable(client):
    api, auth, calls, conn = client
    result = api.get(f"/dashboard/offhours-summary/data?responsible_id={uuid4()}&actor_id={uuid4()}")
    assert result.status_code == 200
    assert auth["session_reads"] == 1
    assert calls == [(conn, auth["id"])]
    assert result.headers["cache-control"].startswith("no-store")
    assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
    assert result.headers["x-content-type-options"] == "nosniff"
    headers = {"If-None-Match": result.headers["etag"]}
    assert api.get("/dashboard/offhours-summary/data", headers=headers).status_code == 304
    assert len(calls) == 2  # Conditional reads still check live scope and message material.
    auth["status"] = "revoked"
    assert api.get("/dashboard/offhours-summary/data", headers=headers).status_code == 403
    assert api.post("/dashboard/offhours-summary/data", json={}).status_code == 405


def test_revoked_session_and_backend_scope_denial_never_render_data(client, monkeypatch):
    api, auth, calls, _ = client
    auth["status"] = "revoked"
    assert api.get("/dashboard/offhours-summary/data").status_code == 403
    assert not calls
    auth["status"] = "active"

    def deny(*_, **kwargs):
        raise PermissionError("当前账号不能查看此总结。")

    monkeypatch.setattr(routes.offhours_summary, "get_summary_view", deny)
    result = api.get("/dashboard/offhours-summary")
    assert result.status_code == 403
    assert auth["session_reads"] == 2
    assert "page" not in result.text
    assert result.headers["cache-control"].startswith("no-store")


def test_source_cm_authorization_loss_returns_forbidden_so_browser_clears_results(client, monkeypatch):
    api, _, _, _ = client

    def revoked_source(*_, **kwargs):
        raise routes.offhours_summary.SummaryInputError("Remi 的消息读取授权已失效。")

    monkeypatch.setattr(routes.offhours_summary, "get_summary_view", revoked_source)
    for path in ("/dashboard/offhours-summary", "/dashboard/offhours-summary/data"):
        result = api.get(path)
        assert result.status_code == 403
        assert result.headers["cache-control"].startswith("no-store")
        assert "page" not in result.text


def test_reply_creation_requires_session_csrf_and_server_scope(client, monkeypatch):
    api, auth, _, _ = client
    captured = []
    monkeypatch.setattr(routes.offhours_summary, "get_settings", lambda: object())
    monkeypatch.setattr(routes.summary_replies, "enqueue", lambda conn, actor, event, settings, **kwargs:
                        captured.append((actor, event)) or "reply-job")
    assert api.post("/dashboard/offhours-summary/replies", json={"event_id": "test:0"}).status_code == 403
    result = api.post("/dashboard/offhours-summary/replies", headers={"X-CSRF-Token": "test-csrf"},
                      json={"event_id": "test:0", "actor_id": str(uuid4())})
    assert result.status_code == 202 and captured == [(auth["id"], "test:0")]
    auth["status"] = "revoked"
    assert api.get("/dashboard/offhours-summary/replies/reply-job").status_code == 403

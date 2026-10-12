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
        importance_metrics={"counts": {"pending": 0, "running": 0, "succeeded": 0, "failed": 0},
                            "usage": {"api_requests": 0, "input_tokens": 0, "output_tokens": 0}},
        summary_settings={"status_label": "未开启", "window_start_input": "2026-09-09T18:00",
                          "window_end_input": "2026-09-10T12:00"},
    )


def test_admin_summary_form_has_native_window_inputs_and_disabled_default_without_identity_threshold():
    html = render_admin()
    forms = re.findall(r'<form action="/admin/ai".*?</form>', html, re.S)
    assert 'name="feature" value="contact_assessment"' not in html
    responsibility_form = next(
        form for form in forms if 'name="feature" value="creator_responsibility_matching"' in form
    )
    assert 'name="auto_confirm_threshold"' in responsibility_form
    group_form = next(form for form in forms if 'name="feature" value="creator_group_classification"' in form)
    assert 'name="auto_confirm_threshold"' in group_form
    assert "自动入库置信度阈值" in group_form
    assert all(
        'name="auto_confirm_threshold"' not in form for form in forms
        if form not in (responsibility_form, group_form)
    )
    import_form = next(form for form in forms if 'name="feature" value="creator_import"' in form)
    assistant_form = next(form for form in forms if 'name="feature" value="operations_assistant"' in form)
    assert 'name="auto_confirm_threshold"' not in assistant_form
    assert 'name="enabled"' not in import_form
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
    assert 'name="rpm_limit"' not in matching_form
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


def test_matching_template_escapes_names_and_distinguishes_associations_from_historical_operator_suggestions():
    environment = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    hostile = "<script>alert(1)</script>"
    display_job = uuid4()
    html = environment.get_template("admin/creator_handle_matching.html").render(
        authorization={"display_name": "Fixture Reviewer"}, admin_landing_path="/admin", admin_role="owner",
        admin_permissions=admin.admin_permissions.PAGE_KEYS, permission_pages=admin.admin_permissions.PAGES,
        active_page="ai", csrf_token="fixture-csrf", matching={
            "enabled": False, "counts": [{"status": "review_required", "n": 2}], "page": 1,
            "jobs": [{"id": uuid4(), "display_name": hostile, "status": "review_required",
                      "reason_code": "suggestions_saved", "input_tokens": 100, "output_tokens": 20,
                      "handle_rows": [
                          {"handle": "fixture.handle", "associated": False, "human_confirmed": False,
                           "conflict": False, "check": {"decision": "review", "relation": "operator",
                                                       "code": "operates_for_other", "period": "historical"}},
                          {"handle": "fixture.owned", "associated": True, "human_confirmed": True,
                           "conflict": False, "check": {"decision": "supported", "relation": "self_account",
                                                       "code": "qa_confirm", "period": "at_message"}},
                          {"handle": "fixture.existing", "associated": True, "human_confirmed": False,
                           "conflict": False, "check": None},
                          {"handle": "fixture.guessed", "associated": False, "human_confirmed": False,
                           "conflict": False, "check": {"decision": "review", "relation": "uncertain",
                                                       "code": "conflict", "period": "unknown"}},
                          {"handle": "fixture.conflicted", "associated": True, "human_confirmed": False,
                           "conflict": True, "check": {"decision": "review", "relation": "uncertain",
                                                      "code": "conflict", "period": "historical",
                                                      "validation_error": "counterevidence"}},
                      ]},
                     {"id": display_job, "display_name": "Display fixture", "status": "review_required",
                      "display_status": "completed", "display_reason_code": "already_confirmed",
                      "reason_code": "suggestions_saved", "input_tokens": 100, "output_tokens": 20,
                      "handle_rows": [{"handle": "fixture.display", "associated": True,
                                       "human_confirmed": False, "check": None, "conflict": False}]}],
        },
    )
    assert "账号匹配" in html and 'action="/admin/ai/creator-handle-matching/enqueue"' in html
    assert "强证据自动关联，其他待核实" in html
    assert hostile not in html and "&lt;script&gt;" in html
    assert "代运营" in html and "历史关系" in html and "待核实" in html
    assert "operator" not in html and "review_required" not in html
    assert 'href="https://www.tiktok.com/@fixture.handle"' in html
    assert "@fixture.owned</a> · 人工已核准 · 本轮：支持</div>" in html
    assert "@fixture.conflicted</a> · 已关联 · 归属冲突 · 本轮：待核实" in html
    assert "@fixture.existing</a> · 已关联</div>" in html
    for handle in ("fixture.handle", "fixture.owned", "fixture.existing", "fixture.guessed", "fixture.conflicted"):
        assert html.count(f'href="https://www.tiktok.com/@{handle}"') == 1
    assert ('<div class="text-secondary"><a class="text-reset" '
            'href="https://www.tiktok.com/@fixture.handle"' in html)
    assert ('<div class="text-danger"><a class="text-reset" '
            'href="https://www.tiktok.com/@fixture.conflicted"' in html)
    assert ('<div class="text-secondary"><a class="text-reset" '
            'href="https://www.tiktok.com/@fixture.guessed"' in html)
    assert html.count('class="text-danger"') == 1
    assert html.count("归属冲突") == 1
    assert "账号关系待核实" in html and "账号归属存在反证" in html and "counterevidence" not in html
    display_row = re.search(r"<tr><td>Display fixture</td>.*?</tr>", html, re.S).group(0)
    assert "已完成" in display_row and "已有正式关联，无需重复匹配" in display_row
    assert f'action="/admin/ai/creator-handle-matching/jobs/{display_job}/cancel"' in display_row
    assert "待核实：2" in html  # The task ledger and controls still use the original status.


def test_group_classification_table_keeps_auto_results_final_and_review_controls_folded():
    from app.creator_group_evidence import PURPOSES

    environment = Environment(loader=FileSystemLoader(APP / "templates"), autoescape=select_autoescape())
    auto_id, review_id = uuid4(), uuid4()
    common = {"status": "completed", "stale": False, "locked": False, "confirmed_tags": [],
              "reason": "Internal diagnostic", "stats": {"included_messages": 42},
              "input_tokens": 2000, "output_tokens": 100}
    html = environment.get_template("admin/creator_groups.html").render(
        authorization={"display_name": "Fixture Reviewer"}, admin_landing_path="/admin", admin_role="owner",
        admin_permissions=admin.admin_permissions.PAGE_KEYS, permission_pages=admin.admin_permissions.PAGES,
        active_page="ai", csrf_token="fixture-csrf", evidence=None, purposes=PURPOSES, matching={
            "enabled": True, "page": 1, "has_more": False,
            "summary_counts": {"classified": 1, "pending": 0, "review": 1, "error": 0},
            "jobs": [
                {**common, "id": auto_id, "chat_id": "oc_auto", "name": "<script>fixture</script>",
                 "confirmed_purpose": "support", "display_purpose": "support", "display_label": "客服群",
                 "display_confidence": "98%", "display_status": "已分类", "display_state": "classified",
                 "result": None, "has_evidence": True, "admission_reason_label": "已符合自动入库规则",
                 "admission_details": {}},
                {**common, "id": review_id, "chat_id": "oc_review", "name": "Review group",
                 "confirmed_purpose": None, "display_purpose": "training", "display_label": "培训群",
                 "display_confidence": "75%", "display_status": "待核实", "display_state": "review",
                 "admission_reason_label": "回复上下文不完整",
                 "admission_details": {"missing_reply_ids": ["M2"],
                                       "missing_reply_reasons": {"M2": "unavailable", "<script>M3</script>": "budget"}},
                 "result": {"purpose": "training", "purpose_label": "培训群", "confidence_label": "75%",
                            "reason": "Long model explanation", "tags": ["writing"], "refs": ["M2"]}},
            ],
        },
    )
    assert "<script>fixture</script>" not in html and "&lt;script&gt;fixture&lt;/script&gt;" in html
    assert "<th>群名</th><th>类型</th><th>置信度</th><th>状态</th><th>操作</th>" in html
    assert "已分类 1" in html and "待核实 1" in html
    assert "98%" in html and "75%" in html
    assert "Long model explanation" not in html and "Internal diagnostic" not in html
    assert "累计 Token" not in html and "证据 42 条" not in html
    assert "正式分类：" not in html and "AI 建议：" not in html and "确认分类" not in html
    auto_row = re.search(r"<tr><td class=\"text-break\">&lt;script&gt;.*?</tr>", html, re.S).group(0)
    assert "采用建议" not in auto_row and "锁定此分类" not in auto_row
    assert re.search(r"<details>.*?/jobs/" + str(auto_id) + r"/evidence.*?</details>", auto_row, re.S)
    assert "<details open" not in html
    review_row = re.search(r'<tr><td class="text-break">Review group.*?</tr>', html, re.S).group(0)
    assert "回复上下文不完整" in review_row.split("<details>", 1)[0]
    assert "M2 · 原消息未同步或不可读" in review_row
    assert "&lt;script&gt;M3&lt;/script&gt; · 上下文超出材料范围" in review_row
    assert "<script>M3</script>" not in html
    assert "原消息未同步或不可读" not in review_row.split("<details>", 1)[0]
    adoption = re.search(r"<form.*?采用建议</button></form>", review_row, re.S).group(0)
    assert f'name="source_job_id" value="{review_id}"' in adoption
    assert 'name="locked"' not in adoption
    assert all('name="csrf_token" value="fixture-csrf"' in form
               for form in re.findall(r"<form .*?</form>", html, re.S))
    evidence_html = environment.get_template("admin/creator_groups.html").render(
        authorization={"display_name": "Fixture Reviewer"}, admin_landing_path="/admin", admin_role="owner",
        admin_permissions=admin.admin_permissions.PAGE_KEYS, permission_pages=admin.admin_permissions.PAGES,
        active_page="ai", purposes=PURPOSES, evidence={
            "name": "Evidence fixture", "admission_reason_label": "回复上下文不完整",
            "stats": {"included_messages": 1, "missing_reply_ids": ["M2"],
                      "missing_reply_reasons": {"M2": "unavailable", "<script>M3</script>": "depth"}},
            "pack": {"chat": {"name": "Evidence fixture", "mode": "group", "member_count": 2},
                     "messages": []},
            "result": {"purpose": "training", "purpose_label": "培训群", "confidence_label": "75%",
                       "reason": "Long model explanation", "tags": []},
        },
    )
    assert "回复上下文不完整" in evidence_html and "M2 · 原消息未同步或不可读" in evidence_html
    assert "&lt;script&gt;M3&lt;/script&gt; · 引用层级超出范围" in evidence_html
    assert "Long model explanation" not in evidence_html and "<script>M3</script>" not in evidence_html


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

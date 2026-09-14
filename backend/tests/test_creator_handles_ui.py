"""Frontend CM review stays separate from owner-only scan management."""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin_permissions, creator_handles, creator_handles_routes, creator_imports


def browser(monkeypatch, *, owner=False, active=True):
    actor = {"id": uuid4(), "status": "active" if active else "revoked", "display_name": "CM"}
    monkeypatch.setattr(admin_permissions, "access_for", lambda _id: {"is_owner": owner})
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
                            autoescape=select_autoescape())
    app = FastAPI()
    app.include_router(creator_handles_routes.create_router(
        current_authorization=lambda _request: actor,
        csrf_token=lambda _auth: "csrf",
        render_template=lambda _request, name, context: HTMLResponse(templates.get_template(name).render(**context)),
    ))
    return TestClient(app), actor


def test_cm_can_read_review_own_handles_without_admin_and_cannot_cross_scope(monkeypatch):
    client, actor = browser(monkeypatch)
    task, foreign = uuid4(), uuid4()
    calls = []

    def page_data(actor_id, *, cm_id, **_kwargs):
        assert actor_id == actor["id"]
        if cm_id not in (None, actor_id):
            raise PermissionError("只能查看自己的达人。")
        return {"cms": [], "selected_cm_id": str(actor_id), "can_manage": False,
                "run": None, "counts": {}, "usage": {}, "items": [], "page": 1, "total_count": 0,
                "total_pages": 0}

    def scoped(actor_id, task_id, *args):
        assert actor_id == actor["id"]
        if task_id != task:
            raise PermissionError("不能查看或操作其他 CM 的达人。")
        calls.append((task_id, *args))
        return {"messages": [{"ref": "G1", "text": "Authorized group name"}]}

    monkeypatch.setattr(creator_handles, "page_data", page_data)
    for name in ("confirm_task", "defer_task", "task_evidence"):
        monkeypatch.setattr(creator_handles, name, scoped)
    html = client.get("/creators")
    assert html.status_code == 200
    assert "候选达人" in html.text and "我的达人" in html.text
    assert "导入 CAP 达人账号名单" in html.text and "整份名单的属性固定为 CAP" in html.text
    assert 'id="creators-type-column" hidden' in html.text
    assert 'id="creators-type-column" hidden' not in client.get("/creators?tab=mine").text
    assert 'id="creators-status-column" hidden' not in html.text
    assert 'id="creators-status-column" hidden' in client.get("/creators?tab=mine").text
    assert 'id="creators-preview"' not in html.text
    assert client.get("/creators/data").status_code == 200
    assert client.get(f"/creators/data?cm_id={foreign}").status_code == 403
    handles = ["example.one", "example_two"]
    assert client.post(f"/creators/tasks/{task}/confirm", data={
        "csrf_token": "csrf", "handles": json.dumps(handles), "actor_id": str(foreign),
    }).status_code == 200
    assert calls == [(task, handles)]
    assert client.post(f"/creators/tasks/{task}/defer", data={"csrf_token": "csrf"}).status_code == 200
    evidence = client.get(f"/creators/tasks/{task}/evidence")
    assert evidence.headers["cache-control"] == "no-store, max-age=0"
    assert evidence.json()["messages"][0]["ref"] == "G1"
    for suffix in ("confirm", "defer"):
        assert client.post(f"/creators/tasks/{foreign}/{suffix}", data={
            "csrf_token": "csrf", "handles": json.dumps(handles),
        }).status_code == 403
    assert client.get(f"/creators/tasks/{foreign}/evidence").status_code == 403
    monkeypatch.setattr(creator_handles, "set_creator_type", lambda *args: calls.append(args))
    type_url = f"/creators/people/{task}/type"
    assert client.post(type_url, data={"cm_id": str(actor["id"]), "creator_type": "cap"}).status_code == 400
    assert client.post(type_url, data={
        "csrf_token": "csrf", "cm_id": str(actor["id"]), "creator_type": "non_cap", "actor_id": str(foreign),
    }).status_code == 410
    assert not any(len(call) == 4 and call[-1] == "non_cap" for call in calls)


def test_retired_ai_and_type_actions_cannot_run_but_still_require_active_auth_and_csrf(monkeypatch):
    client, actor = browser(monkeypatch)
    run, task, batch = uuid4(), uuid4(), uuid4()
    calls = []
    for name in (
        "create_preview", "start_run", "pause_run", "resume_run", "set_auto_admit", "confirm_task", "defer_task",
        "recheck_task", "set_creator_type",
    ):
        monkeypatch.setattr(creator_handles, name, lambda *args: calls.append(args) or run)
    actions = ["/creators/preview", *(f"/creators/runs/{run}/{name}" for name in (
        "start", "pause", "resume", "auto-admit",
    )), f"/creators/tasks/{task}/recheck", f"/creators/people/{task}/type",
        f"/creators/imports/{batch}/pause", f"/creators/imports/{batch}/resume"]
    for path in actions:
        assert client.post(path, data={
            "csrf_token": "csrf", "cm_id": str(actor["id"]), "enabled": "1",
        }).status_code == 410
    for invalid in ({}, {"csrf_token": "wrong", "handles": '["valid"]'},
                    {"csrf_token": "csrf", "handles": '"not-array"'}, {"csrf_token": "csrf", "handles": "[]"}):
        assert client.post(f"/creators/tasks/{task}/confirm", data=invalid).status_code == 400
    owner, _admin = browser(monkeypatch, owner=True)
    for path in actions:
        assert owner.post(path, data={"cm_id": str(actor["id"]), "enabled": "1"}).status_code == 400
        assert owner.post(path, data={"csrf_token": "csrf", "enabled": "1"}).status_code == 410
    assert not calls
    revoked, _ = browser(monkeypatch, active=False)
    assert revoked.get("/creators").status_code == 403
    for path in actions:
        assert revoked.post(path, data={"csrf_token": "csrf"}).status_code == 403
    assert revoked.post(f"/creators/tasks/{task}/defer", data={"csrf_token": "csrf"}).status_code == 403


def test_excel_upload_is_bounded_scoped_and_requires_explicit_start_and_confirmation(monkeypatch):
    client, actor = browser(monkeypatch)
    batch, row, person, foreign = uuid4(), uuid4(), uuid4(), uuid4()
    calls = []

    def preview(actor_id, cm_id, filename, content, creator_type="cap"):
        if cm_id != actor_id:
            raise PermissionError("只能导入自己的名单。")
        calls.append(("preview", actor_id, filename, content, creator_type))
        return {"id": str(batch), "status": "preview", "counts": {"valid": 1, "duplicates": 0, "invalid": 0},
                "rows": [{"row_number": 2, "handle": "example", "error": ""}]}

    monkeypatch.setattr(creator_imports, "preview_import", preview)
    monkeypatch.setattr(creator_imports, "start_import", lambda *args: calls.append(("start", *args)))
    monkeypatch.setattr(creator_imports, "confirm_row", lambda *args: calls.append(("confirm", *args)))
    url = f"/creators/imports/preview?cm_id={actor['id']}"
    headers = {"Content-Type": "application/octet-stream", "X-Filename": "CAP%20handles.xlsx"}
    assert client.post(url, content=b"xlsx", headers=headers).status_code == 400
    headers["X-CSRF-Token"] = "csrf"
    assert client.post(url, content=b"x" * (creator_imports.MAX_FILE_BYTES + 1), headers=headers).status_code == 400
    assert client.post(url, content=iter([b"x" * (creator_imports.MAX_FILE_BYTES + 1)]),
                       headers=headers).status_code == 400
    assert not calls
    assert client.post(f"/creators/imports/preview?cm_id={foreign}", content=b"xlsx",
                       headers=headers).status_code == 403
    result = client.post(url, content=b"xlsx", headers=headers)
    assert result.status_code == 200 and result.json()["counts"]["valid"] == 1
    assert result.headers["cache-control"] == "no-store, max-age=0"
    assert calls == [("preview", actor["id"], "CAP handles.xlsx", b"xlsx", "cap")]
    for creator_type in ("non_cap", "pending"):
        assert client.post(url + "&creator_type=" + creator_type, content=b"xlsx", headers=headers).status_code == 400
    assert len(calls) == 1
    assert client.post(f"/creators/imports/{batch}/start").status_code == 400
    assert client.post(f"/creators/imports/{batch}/start", data={"csrf_token": "csrf"}).status_code == 200
    assert calls[-1] == ("start", actor["id"], batch)
    confirm = f"/creators/imports/rows/{row}/confirm"
    assert client.post(confirm, data={"handle": "example", "contact_id": str(person)}).status_code == 400
    assert client.post(confirm, data={"csrf_token": "csrf", "handle": "example"}).status_code == 400
    assert client.post(confirm, data={"csrf_token": "csrf", "handle": "example", "contact_id": str(person),
                                      "actor_id": str(foreign)}).status_code == 200
    assert calls[-1] == ("confirm", actor["id"], row, "example", person)

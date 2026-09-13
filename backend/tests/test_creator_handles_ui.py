"""Frontend CM review stays separate from owner-only scan management."""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin_permissions, creator_handles, creator_handles_routes


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


def test_scan_and_auto_admit_are_owner_only_and_explicit_csrf_is_required(monkeypatch):
    client, actor = browser(monkeypatch)
    run, task = uuid4(), uuid4()
    calls = []
    for name in (
        "create_preview", "start_run", "pause_run", "resume_run", "set_auto_admit", "confirm_task", "defer_task",
        "recheck_task",
    ):
        monkeypatch.setattr(creator_handles, name, lambda *args: calls.append(args) or run)
    actions = ["/creators/preview", *(f"/creators/runs/{run}/{name}" for name in (
        "start", "pause", "resume", "auto-admit",
    )), f"/creators/tasks/{task}/recheck"]
    for path in actions:
        assert client.post(path, data={
            "csrf_token": "csrf", "cm_id": str(actor["id"]), "enabled": "1",
        }).status_code == 403
    for invalid in ({}, {"csrf_token": "wrong", "handles": '["valid"]'},
                    {"csrf_token": "csrf", "handles": '"not-array"'}, {"csrf_token": "csrf", "handles": "[]"}):
        assert client.post(f"/creators/tasks/{task}/confirm", data=invalid).status_code == 400
    assert not calls
    owner, admin = browser(monkeypatch, owner=True)
    for path in actions:
        assert owner.post(path, data={"cm_id": str(actor["id"]), "enabled": "1"}).status_code == 400
    assert not calls
    assert owner.post(f"/creators/runs/{run}/auto-admit", data={"csrf_token": "csrf"}).status_code == 400
    assert owner.post(f"/creators/runs/{run}/auto-admit", data={
        "csrf_token": "csrf", "enabled": "0",
    }).status_code == 200
    assert calls == [(admin["id"], run, False)]
    assert owner.post(f"/creators/tasks/{task}/recheck", data={"csrf_token": "csrf"}).status_code == 200
    assert calls == [(admin["id"], run, False), (admin["id"], task)]

    def unchanged(*_args):
        raise ValueError("材料未变化，不会重复付费。")

    monkeypatch.setattr(creator_handles, "recheck_task", unchanged)
    response = owner.post(f"/creators/tasks/{task}/recheck", data={"csrf_token": "csrf"})
    assert response.status_code == 400 and "材料未变化" in response.json()["error"]
    assert calls == [(admin["id"], run, False), (admin["id"], task)]
    revoked, _ = browser(monkeypatch, active=False)
    assert revoked.get("/creators").status_code == 403
    assert revoked.post(f"/creators/tasks/{task}/defer", data={"csrf_token": "csrf"}).status_code == 403

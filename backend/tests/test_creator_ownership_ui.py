"""One boundary suite: read-only grants, paid actions/CSRF, and private evidence."""

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin, admin_permissions, creator_ownership

BASE = "/admin/ai/creator-ownership"


def client(monkeypatch, owner=False, grants=("ai",), session=True):
    actor = {"id": uuid4(), "status": "active", "display_name": "Tester"}
    acl = admin_permissions._access({
        "status": "active", "role": "owner" if owner else None,
        "role_active": True, "permissions": list(grants),
    })
    monkeypatch.setattr(admin_permissions, "access_for", lambda _id: acl)
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
        autoescape=select_autoescape(),
    )
    app = FastAPI()
    app.include_router(admin.create_admin_router(
        current_authorization=lambda _request: actor if session else None,
        csrf_token=lambda _actor: "csrf",
        render_template=lambda _request, name, context, status_code=200: HTMLResponse(
            templates.get_template(name).render(**context), status_code=status_code,
        ),
    ))
    return TestClient(app), actor


def stub_page(monkeypatch):
    reads = []
    cm, run, task = uuid4(), uuid4(), uuid4()

    def page_data(**kwargs):
        reads.append(kwargs)
        return {
            "cms": [{"id": str(cm), "name": "Remi"}], "selected_cm_id": str(cm),
            "run": {"id": str(run), "status": "preview", "can_start": True},
            "counts": {"total": 1, "ready": 1}, "usage": {"requests": 0},
            "items": [{"id": str(task), "display_name": "<script>alert(1)</script>",
                       "open_id": "ou_person", "status": "ready", "reason": "业务材料已就绪"}],
            "page": 1, "total_pages": 1, "total_count": 1,
        }

    monkeypatch.setattr(creator_ownership, "page_data", page_data)
    return reads, cm, run, task


def test_read_only_permission_never_reads_private_evidence_or_mutates(monkeypatch):
    reads, cm, run, task = stub_page(monkeypatch)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Read-only viewers must never enter private or write services")

    for name in (
        "create_preview", "start_run", "pause_run", "resume_run", "confirm_tasks", "defer_tasks", "task_evidence",
    ):
        monkeypatch.setattr(creator_ownership, name, unexpected)
    browser, _ = client(monkeypatch)
    page = browser.get(BASE)
    assert page.status_code == 200
    assert "只读查看判断结果" in page.text
    assert 'id="ownership-start"' not in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "\\u003cscript\\u003e" in page.text
    for _ in range(2):
        response = browser.get(f"{BASE}/data?cm_id={cm}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0"
    assert all(not read["include_private"] for read in reads)
    assert browser.get(f"{BASE}/tasks/{task}/evidence").status_code == 403
    for action in ("start", "pause", "resume", "confirm", "defer"):
        assert browser.post(f"{BASE}/{run}/{action}", data={
            "csrf_token": "csrf", "limit": "20", "task_ids": str(task),
        }).status_code == 403
    assert browser.post(f"{BASE}/preview", data={"csrf_token": "csrf", "cm_id": str(cm)}).status_code == 403
    denied, _ = client(monkeypatch, grants=("contacts",))
    assert denied.get(BASE).status_code == denied.get(f"{BASE}/data").status_code == 403
    signed_out, _ = client(monkeypatch, session=False)
    assert signed_out.get(BASE, follow_redirects=False).status_code == 303


def test_owner_actions_require_csrf_valid_ids_and_explicit_paid_limit(monkeypatch):
    reads, cm, run, task = stub_page(monkeypatch)
    writes = []
    monkeypatch.setattr(creator_ownership, "create_preview", lambda *args: writes.append(("preview", args)) or run)
    monkeypatch.setattr(creator_ownership, "start_run", lambda *args, limit: writes.append(("start", args, limit)))
    monkeypatch.setattr(creator_ownership, "confirm_tasks", lambda *args: writes.append(("confirm", args)) or 1)
    monkeypatch.setattr(creator_ownership, "defer_tasks", lambda *args: writes.append(("defer", args)) or 1)
    monkeypatch.setattr(creator_ownership, "pause_run", lambda *args: writes.append(("pause", args)))
    monkeypatch.setattr(creator_ownership, "resume_run", lambda *args: writes.append(("resume", args)))
    browser, actor = client(monkeypatch, owner=True)
    html = browser.get(BASE)
    assert html.status_code == 200
    assert 'id="ownership-confirm"' not in html.text
    assert "查看前台候选" in html.text
    assert reads[-1]["include_private"] is True
    assert not writes
    bypass = browser.post(f"{BASE}/{run}/confirm", data={"csrf_token": "csrf", "task_ids": str(task)})
    assert bypass.status_code == 400
    assert "前台候选" in bypass.json()["error"]
    assert not writes
    for action in ("start", "pause", "resume", "confirm", "defer"):
        assert browser.post(f"{BASE}/{run}/{action}", data={"csrf_token": "wrong"}).status_code == 400
    assert browser.post(f"{BASE}/preview", data={"cm_id": str(cm)}).status_code == 400
    assert browser.post(f"{BASE}/{run}/start", data={"csrf_token": "csrf"}).status_code == 400
    for ids in ("", "invalid", f"{task},{task}"):
        assert browser.post(f"{BASE}/{run}/confirm", data={"csrf_token": "csrf", "task_ids": ids}).status_code == 400
    assert not writes
    assert browser.post(f"{BASE}/preview", data={"csrf_token": "csrf", "cm_id": str(cm)}).json()["run_id"] == str(run)
    for limit in ("20", "all"):
        assert browser.post(f"{BASE}/{run}/start", data={"csrf_token": "csrf", "limit": limit}).status_code == 200
    for action in ("defer",):
        result = browser.post(f"{BASE}/{run}/{action}", data={"csrf_token": "csrf", "task_ids": str(task)})
        assert result.json()["count"] == 1
    for action in ("pause", "resume"):
        assert browser.post(f"{BASE}/{run}/{action}", data={"csrf_token": "csrf"}).status_code == 200
    assert writes == [
        ("preview", (actor["id"], cm)), ("start", (actor["id"], run), 20),
        ("start", (actor["id"], run), None), ("defer", (actor["id"], run, [task])),
        ("pause", (actor["id"], run)), ("resume", (actor["id"], run)),
    ]


def test_private_evidence_rechecks_access_and_uses_no_store(monkeypatch):
    _reads, cm, run, task = stub_page(monkeypatch)
    browser, actor = client(monkeypatch, owner=True)
    seen = []
    monkeypatch.setattr(creator_ownership, "task_evidence", lambda *args: seen.append(args) or {
        "cm_id": str(cm), "cm_name": "Remi", "display_name": "Creator",
        "messages": [{"ref": "M12", "text": "Private conversation", "available": True},
                     {"ref": "M2", "text": "Original U1 wording", "available": True}],
    })
    response = browser.get(f"{BASE}/tasks/{task}/evidence")
    assert response.status_code == 200
    assert seen == [(actor["id"], task)]
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.json()["cm_id"] == str(cm)
    assert response.json()["cm_name"] == "Remi"
    assert [message["ref"] for message in response.json()["messages"]] == ["M12", "M2"]
    assert response.json()["messages"][1]["text"] == "Original U1 wording"
    for error, status in ((ValueError, 400), (LookupError, 404), (PermissionError, 403)):
        def reject(*_args, error=error, **_kwargs):
            raise error("无法读取")
        monkeypatch.setattr(creator_ownership, "task_evidence", reject)
        result = browser.get(f"{BASE}/tasks/{task}/evidence")
        assert result.status_code == status
        assert result.json() == {"error": "无法读取"}

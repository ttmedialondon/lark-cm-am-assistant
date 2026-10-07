"""Frontend CM review stays separate from owner-only scan management."""

import asyncio
import json
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import admin_permissions, creator_broadcast, creator_handles_routes, creator_imports


def browser(monkeypatch, *, owner=False, active=True):
    actor = {"id": uuid4(), "status": "active" if active else "revoked", "display_name": "CM"}
    monkeypatch.setattr(admin_permissions, "access_for", lambda _id: {"is_owner": owner})
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "app/templates"),
                            autoescape=select_autoescape())
    app = FastAPI()
    def authorization(_request):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        actor["session_reads"] = actor.get("session_reads", 0) + 1
        return actor

    app.include_router(creator_handles_routes.create_router(
        current_authorization=authorization,
        csrf_token=lambda _auth: "csrf",
        render_template=lambda _request, name, context: HTMLResponse(templates.get_template(name).render(**context)),
    ))
    return TestClient(app), actor


def test_broadcast_real_data_workflow_rechecks_scope_routes_and_never_sends(monkeypatch):
    client, actor = browser(monkeypatch, owner=True)
    p1, p2, other_cm = map(str, (uuid4(), uuid4(), uuid4()))
    people = [{"id": p1, "name": "Example One", "handles": ["example_one", "example_backup"]},
              {"id": p2, "name": "Example Two", "handles": ["example_two"]}]
    group = {"id": "oc_example", "name": "Example group", "purpose": "support", "members": [p1, p2],
             "member_count": None, "membership_note": creator_broadcast.MEMBERSHIP_NOTE}
    changed = {"routes": False, "role": True}
    reads = []
    saved_lists = {}

    class Rows:
        def __init__(self, values):
            self.values = values
        def fetchone(self):
            return self.values[0] if self.values else None
        def fetchall(self):
            return self.values

    class Conn:
        def transaction(self):
            return nullcontext()
        def execute(self, sql, params=()):
            reads.append(sql)
            if "creator_broadcast_lists" in sql:
                if sql.startswith("INSERT"):
                    name, notes, cm, purpose, mention, targets, snapshot, owner = params
                    item = {"id": uuid4(), "name": name, "notes": notes, "cm_id": cm,
                            "purpose": purpose, "mention": mention, "targets": targets.obj,
                            "snapshot": snapshot.obj, "owner_authorization_id": owner, "revision": 1,
                            "deleted_at": None}
                    saved_lists[str(item["id"])] = item
                    return Rows([dict(item)])
                if sql.startswith("UPDATE"):
                    item = saved_lists.get(str(params[-3]))
                    if (not item or str(item["owner_authorization_id"]) != str(params[-2])
                            or item["revision"] != params[-1] or item["deleted_at"]):
                        return Rows([])
                    if "SET deleted_at" in sql:
                        item["deleted_at"] = True
                    elif "cm_id=%s" in sql:
                        name, notes, cm, purpose, mention, targets, snapshot = params[:7]
                        item.update(name=name, notes=notes, cm_id=cm, purpose=purpose, mention=mention,
                                    targets=targets.obj, snapshot=snapshot.obj)
                    else:
                        item.update(name=params[0], notes=params[1])
                    item["revision"] += 1
                    return Rows([dict(item)])
                if "WHERE id=%s" in sql:
                    item = saved_lists.get(str(params[0]))
                    owned = item and str(item["owner_authorization_id"]) == str(params[1]) and not item["deleted_at"]
                    return Rows([dict(item)] if owned else [])
                return Rows([dict(item) for item in saved_lists.values()
                             if str(item["owner_authorization_id"]) == str(params[0]) and not item["deleted_at"]])
            if sql == "SET TRANSACTION READ ONLY":
                return Rows([])
            if "JOIN admin_roles" in sql:
                return Rows([{"owner": True}] if changed["role"] and str(params[0]) == str(actor["id"]) else [])
            if "SELECT id,display_name" in sql:
                return Rows([{"id": actor["id"], "display_name": "CM"}])
            if "SELECT 1 FROM authorizations" in sql:
                return Rows([{"active": True}])
            if "SELECT id,open_id,display_name" in sql:
                return Rows([{"id": params[0], "open_id": "ou_example", "display_name": "CM"}])
            raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(creator_broadcast, "connection", lambda: nullcontext(Conn()))
    monkeypatch.setattr(creator_broadcast, "_people", lambda _conn, cm, ids=None: [
        person for person in people if (ids is None or person["id"] in ids) and (cm is None or person["id"] == p1)
    ])
    monkeypatch.setattr(creator_broadcast, "_groups", lambda _conn, ids, _cm, purpose: [dict(group, purpose=purpose,
                        members=[contact for contact in group["members"] if contact in ids])]
                        if ids and not changed["routes"] else [])
    response = client.get("/creators/broadcast")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert "真实数据 · 仅预览" in response.text
    assert "发送（未开通）" in response.text and "creator-broadcast.mjs" in response.text
    for label in creator_broadcast.PURPOSES.values():
        assert label in response.text
    assert client.post("/creators/broadcast").status_code == 405
    headers = {"X-CSRF-Token": "csrf"}
    request = {"cm_id": "all", "purpose": "support", "raw": "@example_one\n@example_backup\n@example_two"}
    url = "/creators/broadcast/match"
    assert client.post(url, json=request).status_code == 400
    result = client.post(url, json=request, headers=headers)
    assert result.status_code == 200 and len(result.json()["entries"]) == 3
    assert result.headers["cache-control"] == "no-store, max-age=0"
    assert result.json()["can_send"] is False
    preview = {"cm_id": "all", "purpose": "support", "message": "Example\nhttps://example.com", "mention": True,
               "selections": [{"contact_id": p1, "chat_id": group["id"]},
                              {"contact_id": p1, "chat_id": group["id"]},
                              {"contact_id": p2, "chat_id": group["id"]}]}
    for purpose in creator_broadcast.PURPOSES:
        preview["purpose"] = purpose
        checked = client.post("/creators/broadcast/preview", json=preview, headers=headers).json()
        assert checked["issues"] == [] and len(checked["groups"]) == 1
        assert len(checked["groups"][0]["mentions"]) == 2 and checked["can_send"] is False
    preview.update(cm_id=other_cm, actor_id=other_cm)
    scoped = client.post("/creators/broadcast/preview", json=preview, headers=headers).json()
    assert len(scoped["issues"]) == 1 and len(scoped["groups"][0]["mentions"]) == 1
    changed["routes"] = True
    assert client.post("/creators/broadcast/preview", json=preview, headers=headers).json()["groups"] == []
    for invalid in (dict(request, purpose="unknown"), dict(request, raw=123), dict(request, raw="name\n" * 501)):
        assert client.post(url, json=invalid, headers=headers).status_code == 400
    json_headers = dict(headers, **{"Content-Type": "application/json"})
    assert client.post(url, content=b"x" * 300001, headers=json_headers).status_code == 400
    assert client.post(url, content=iter([b"x" * 300001]), headers=json_headers).status_code == 400
    assert client.post(url, content=b"{", headers=json_headers).status_code == 400
    assert client.post("/creators/broadcast/send", json=preview, headers=headers).status_code == 404
    assert any(sql == "SET TRANSACTION READ ONLY" for sql in reads)
    changed["routes"] = False
    preview.update(cm_id="all", purpose="support")
    resolve_url = "/creators/broadcast/resolve"
    resolved = client.post(resolve_url, json={"cm_id": "all", "purpose": "support",
                                             "contact_ids": [p1, p2, p1]}, headers=headers).json()
    assert len(resolved["entries"]) == 2 and resolved["duplicate_inputs"] == 1
    for scope in (None, "", "bad-uuid"):
        denied_scope = client.post(resolve_url, json={"cm_id": scope, "purpose": "support", "contact_ids": [p1]},
                                   headers=headers)
        assert denied_scope.status_code == 400
    lists_url = "/creators/broadcast/lists/"
    fields = {"name": "Example static list", "notes": "Example", "cm_id": "all", "purpose": "support",
              "mention": True, "selections": preview["selections"], "message": "must not be saved"}
    assert client.post(lists_url + "save", json=fields).status_code == 400
    for invalid in (dict(fields, name="bad\x00name"), dict(fields, notes="bad\x00notes")):
        assert client.post(lists_url + "save", json=invalid, headers=headers).status_code == 400
    invalid_unicode = json.dumps(dict(fields, name="bad\ud800name"))
    assert client.post(lists_url + "save", content=invalid_unicode,
                       headers=dict(headers, **{"Content-Type": "application/json"})).status_code == 400
    created = client.post(lists_url + "save", json=fields, headers=headers)
    assert created.status_code == 200
    item = created.json()["list"]
    stored = saved_lists[item["id"]]
    assert len(stored["targets"]) == 2 and "message" not in stored["snapshot"]
    assert item["target_count"] == 2
    loaded = client.post(lists_url + "load", json={"id": item["id"]}, headers=headers).json()
    assert loaded["issues"] == [] and len(loaded["routes"]) == 2
    changed["routes"] = True
    stale = client.post(lists_url + "load", json={"id": item["id"]}, headers=headers).json()
    assert len(stale["issues"]) == 2 and all(not route["chat_id"] for route in stale["routes"])
    assert all(not entry["selected"] for entry in stale["entries"])
    metadata = dict(fields, id=item["id"], revision=1, metadata_only=True, name="Renamed", cm_id="invalid",
                    selections=[], mention="invalid", purpose="invalid")
    renamed = client.post(lists_url + "save", json=metadata, headers=headers)
    assert renamed.status_code == 200 and renamed.json()["list"]["revision"] == 2
    assert stored["purpose"] == "support" and len(stored["targets"]) == 2
    assert client.post(lists_url + "save", json=metadata, headers=headers).status_code == 400
    invalid_update = client.post(lists_url + "save", json=dict(fields, id=item["id"], revision=2), headers=headers)
    assert invalid_update.status_code == 400
    changed["routes"] = False
    updated = client.post(lists_url + "save", json=dict(fields, id=item["id"], revision=2,
                           selections=[{"contact_id": p1, "chat_id": group["id"]}]), headers=headers)
    assert updated.status_code == 200 and updated.json()["list"]["target_count"] == 1, updated.text
    stored["owner_authorization_id"] = other_cm
    for action in ("load", "delete", "save"):
        foreign = client.post(lists_url + action, json=dict(fields, id=item["id"], revision=3), headers=headers)
        assert foreign.status_code == 404
    stored["owner_authorization_id"] = actor["id"]
    assert client.post(lists_url + "delete", json={"id": item["id"], "revision": 2}, headers=headers).status_code == 400
    deleted = client.post(lists_url + "delete", json={"id": item["id"], "revision": 3}, headers=headers)
    assert deleted.status_code == 200 and deleted.json()["lists"] == [] and stored["deleted_at"]
    assert client.post(lists_url + "load", json={"id": item["id"]}, headers=headers).status_code == 404
    changed["role"] = False
    assert client.get("/creators/broadcast").status_code == 403
    assert client.post(url, json=request, headers=headers).status_code == 403
    assert client.post(lists_url + "save", json=fields, headers=headers).status_code == 403
    ordinary, _ = browser(monkeypatch)
    assert ordinary.get("/creators/broadcast").status_code == 403
    revoked, _ = browser(monkeypatch, owner=True, active=False)
    assert revoked.get("/creators/broadcast").status_code == 403
    app = FastAPI()
    app.include_router(creator_handles_routes.create_router(
        current_authorization=lambda _: None, csrf_token=lambda _: "",
        render_template=lambda *_: pytest.fail("Unauthenticated prototype was rendered"),
    ))
    denied = TestClient(app).get("/creators/broadcast", follow_redirects=False)
    assert (denied.status_code, denied.headers["location"]) == (303, "/")




def test_retired_handle_ai_and_type_actions_still_require_active_auth_and_csrf(monkeypatch):
    client, actor = browser(monkeypatch)
    run, task = uuid4(), uuid4()
    actions = [
        "/creators/preview",
        *(
            f"/creators/runs/{run}/{name}"
            for name in (
                "start",
                "pause",
                "resume",
                "auto-admit",
            )
        ),
        *(f"/creators/tasks/{task}/{name}" for name in ("recheck", "confirm", "defer")),
        f"/creators/people/{task}/type",
    ]
    for path in actions:
        assert client.post(path, data={
            "csrf_token": "csrf", "cm_id": str(actor["id"]), "enabled": "1",
        }).status_code == 410
    for invalid in ({}, {"csrf_token": "wrong"}):
        assert client.post(f"/creators/tasks/{task}/confirm", data=invalid).status_code == 400
    owner, _admin = browser(monkeypatch, owner=True)
    for path in actions:
        assert owner.post(path, data={"cm_id": str(actor["id"]), "enabled": "1"}).status_code == 400
        assert owner.post(path, data={"csrf_token": "csrf", "enabled": "1"}).status_code == 410
    evidence = client.get(f"/creators/tasks/{task}/evidence")
    assert evidence.status_code == 410
    assert evidence.headers["cache-control"] == "no-store, max-age=0"
    revoked, _ = browser(monkeypatch, active=False)
    assert revoked.get("/creators").status_code == 403
    assert revoked.get(f"/creators/tasks/{task}/evidence").status_code == 403
    for path in actions:
        assert revoked.post(path, data={"csrf_token": "csrf"}).status_code == 403
    assert revoked.post(f"/creators/tasks/{task}/defer", data={"csrf_token": "csrf"}).status_code == 403


def test_excel_upload_is_bounded_scoped_and_requires_explicit_start_and_confirmation(monkeypatch):
    client, actor = browser(monkeypatch)
    batch, row, person, foreign = uuid4(), uuid4(), uuid4(), uuid4()
    calls = []

    def preview(actor_id, cm_id, filename, content):
        if cm_id != actor_id:
            raise PermissionError("只能导入自己的名单。")
        calls.append(("preview", actor_id, filename, content))
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
    assert calls == [("preview", actor["id"], "CAP handles.xlsx", b"xlsx")]
    for creator_type in ("cap", "non_cap", "pending"):
        assert client.post(url + "&creator_type=" + creator_type, content=b"xlsx", headers=headers).status_code == 200
    assert len(calls) == 4
    assert client.post(f"/creators/imports/{batch}/start").status_code == 400
    assert client.post(f"/creators/imports/{batch}/start", data={"csrf_token": "csrf"}).status_code == 200
    assert calls[-1] == ("start", actor["id"], batch)
    confirm = f"/creators/imports/rows/{row}/confirm"
    assert client.post(confirm, data={"handle": "example", "contact_id": str(person)}).status_code == 400
    assert client.post(confirm, data={"csrf_token": "csrf", "handle": "example"}).status_code == 400
    assert client.post(confirm, data={"csrf_token": "csrf", "handle": "example", "contact_id": str(person),
                                      "actor_id": str(foreign)}).status_code == 200
    assert calls[-1] == ("confirm", actor["id"], row, "example", person)
    for action, name in (("pause", "pause_import"), ("resume", "resume_import"), ("recheck", "recheck_batch")):
        def scoped(actor_id, batch_id, *, action=action):
            if batch_id != batch:
                raise PermissionError("只能操作自己的导入批次。")
            calls.append((action, actor_id, batch_id))

        monkeypatch.setattr(creator_imports, name, scoped)
        action_url = f"/creators/imports/{batch}/{action}"
        assert client.post(action_url).status_code == 400
        assert client.post(action_url, data={"csrf_token": "csrf", "actor_id": str(foreign)}).status_code == 200
        assert calls[-1] == (action, actor["id"], batch)
        assert client.post(f"/creators/imports/{foreign}/{action}", data={"csrf_token": "csrf"}).status_code == 403

    def evidence(actor_id, row_id):
        assert actor_id == actor["id"]
        if row_id != row:
            raise PermissionError("不能读取其他 CM 的材料。")
        return {"handle": "example", "stages": [{"stage": "group", "messages": []}]}

    monkeypatch.setattr(creator_imports, "row_evidence", evidence)
    material = client.get(f"/creators/imports/rows/{row}/evidence")
    assert material.status_code == 200 and material.json()["stages"][0]["stage"] == "group"
    assert material.headers["cache-control"] == "no-store, max-age=0"
    assert client.get(f"/creators/imports/rows/{foreign}/evidence").status_code == 403
    revoked, _ = browser(monkeypatch, active=False)
    assert revoked.get(f"/creators/imports/rows/{row}/evidence").status_code == 403
    assert revoked.post(f"/creators/imports/{batch}/recheck", data={"csrf_token": "csrf"}).status_code == 403
